from abc import abstractmethod
import math
from dataclasses import dataclass, field
from toolz.dicttoolz import valmap
from typing import (
    Callable,
    Dict,
    Hashable,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)
import numpy as np
import torch
import torch.nn as nn
from torch.nn import functional as F
from torch.utils.data import random_split
import pytorch_lightning as pl
from tqdm import tqdm
import swyft
import swyft.utils
from swyft.inference.marginalratioestimator import get_ntrain_nvalid
import yaml

import zarr
import fasteners
from dataclasses import dataclass
from pytorch_lightning import loggers as pl_loggers

from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint
from pytorch_lightning import loggers as pl_loggers
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
import pytorch_lightning as pl
from pytorch_lightning.trainer.supporters import CombinedLoader

from swyft.networks.standardization import OnlineStandardizingLayer


###########
# Simulator
###########

def to_numpy(x, single_precision = False):

    if isinstance(x, torch.Tensor):
        if not single_precision:
            return x.detach().cpu().numpy()
        else:
            x = x.detach().cpu()
            if x.dtype == torch.float64:
                x = x.float().numpy()
            else:
                x = x.numpy()
            return x
    elif isinstance(x, Samples):
        return Samples({k: to_numpy(v, single_precision = single_precision) for k, v in x.items()})
    elif isinstance(x, dict):
        return {k: to_numpy(v, single_precision = single_precision) for k, v in x.items()}
    elif isinstance(x, np.ndarray):
        if not single_precision:
            return x
        else:
            if x.dtype == np.float64:
                x = np.float32(x)
            return x
    else:
        return x

def to_numpy32(x):
    return to_numpy(x, single_precision = True)
    
def to_torch(x):
    if isinstance(x, Samples):
        return Samples({k: to_torch(v) for k, v in x.items()})
    elif isinstance(x, dict):
        return {k: to_torch(v) for k, v in x.items()}
    else:
        return torch.as_tensor(x)
    

class Trace(dict):
    """Defines the computational graph (DAG) and keeps track of simulation results.
    """
    def __init__(self, targets = None, conditions = {}, log_prob = False):
        """Instantiate Trace instante.

        Args:
            targets: Optional list of target sample variables. If provided, execution is stopped after those targets are evaluated. If `None`, all variables in the DAG will be evaluated.
            conditions: Optional `dict` or Callable. If a `dict`, sample variables will be conditioned to the corresponding values.  If Callable, it will be evaulated and it is expected to return a `dict`.
            log_prob: Boolean. Collect log_prob values if available. Default False.
        """

        super().__init__(conditions)
        self._targets = targets
        self._prefix = ""
        self._eval_log_probs = log_prob

    def __repr__(self):
        return "Trace("+super().__repr__()+")"

    def __setitem__(self, k, v):
        if k not in self.keys():
            super().__setitem__(k, v)

    @property
    def covers_targets(self):
        return (self._targets is not None 
                and all([k in self.keys() for k in self._targets]))

    def sample(self, names, fn, *args, **kwargs):
        """Register sampling function.

        Args:
            names: Name or list of names of sampling variables.
            fn: Callable that returns the (list of) sampling variable(s).
            *args, **kwargs: Arguments and keywords arguments that are passed to `fn` upon evaluation.  LazyValues will be automatically evaluated if necessary.

        Returns:
            LazyValue sample.
        """
        assert callable(fn), "Second argument must be a function."
        return self._sample(names, fn, False, *args, **kwargs)

    def sample_dist(self, names, fn, *args, **kwargs):
        return self._sample(names, fn, True, *args, **kwargs)

    def _sample(self, names, fn, dist, *args, **kwargs):
        if isinstance(names, list):
            names = [self._prefix + n for n in names]
            lazy_values = [LazyValue(self, k, names, fn, dist, *args, **kwargs) for k in names]
            if self._targets is None or any([k in self._targets for k in names]):
                lazy_values[0].evaluate()
            return tuple(lazy_values)
        elif isinstance(names, str):
            name = self._prefix + names
            lazy_value = LazyValue(self, name, name, fn, dist, *args, **kwargs)
            if self._targets is None or name in self._targets:
                lazy_value.evaluate()
            return lazy_value
        else:
            raise ValueError

    def prefix(self, prefix):
        return TracePrefixContextManager(self, prefix)


class TracePrefixContextManager:
    def __init__(self, trace, prefix):
        self._trace = trace
        self._prefix = prefix

    def __enter__(self):
        self._prefix, self._trace._prefix = self._trace._prefix, self._prefix + self._trace._prefix

    def __exit__(self, exception_type, exception_value, traceback):
        self._trace._prefix = self._prefix


class LazyValue:
    """Provides lazy evaluation functionality.
    """
    def __init__(self, trace, this_name, fn_out_names, fn, dist, *args, **kwargs):
        """Instantiates LazyValue object.

        Args:
            trace: Trace instance (to be populated with sample).
            this_name: Name of this the variable that this LazyValue represents.
            fn_out_names: Name or list of names of variables that `fn` returns.
            fn: Callable or object that (upon instantiation) returns sample or list of samples.
            dist: Type of fn: Callable (false) or distribution (true)
            args, kwargs: Arguments and keyword arguments provided to `fn` upon evaluation.
        """
        self._trace = trace
        self._dist = dist
        self._this_name = this_name
        self._fn_out_names = fn_out_names
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def __repr__(self):
        value = self._trace[self._this_name] if self._this_name in self._trace.keys() else "None"
        return f"LazyValue{self._this_name, value, self._fn, self._args, self._kwargs}"

    @property
    def value(self):
        """Value of this object."""
        return self.evaluate()

    def evaluate(self):
        """Trigger evaluation of function.

        Returns:
            Value of `this_name`.
        """
        instance = None
        if self._this_name not in self._trace.keys():
            args = (arg.evaluate() if isinstance(arg, LazyValue) else arg for arg in self._args)
            kwargs = {k: v.evaluate() if isinstance(v, LazyValue) else v for k, v in self._kwargs.items()}
            if not self._dist:
                result = self._fn(*args, **kwargs)
            else:
                instance = self._fn(*args, **kwargs)
                result = instance.sample()
            if not isinstance(self._fn_out_names, list):
                self._trace[self._fn_out_names] = result
            else:
                for out_name, value in zip(self._fn_out_names, result):
                    self._trace[out_name] = value
        if self._trace._eval_log_probs and self._this_name and self._dist:
            if instance is None:
                args = (arg.evaluate() if isinstance(arg, LazyValue) else arg for arg in self._args)
                kwargs = {k: v.evaluate() if isinstance(v, LazyValue) else v for k, v in self._kwargs.items()}
                instance = self._fn(*args, **kwargs)
            result = instance.log_prob(self._trace[self._this_name])
            self._trace[self._this_name + ":log_prob"] = result
        return self._trace[self._this_name]


def collate_output(out):
    """Turn list of tensors/arrays-value dicts into dict of collated tensors or arrays"""
    keys = out[0].keys()
    result = {}
    for key in keys:
        if isinstance(out[0][key], torch.Tensor):
            result[key] = torch.stack([x[key] for x in out])
        else:
            result[key] = np.stack([x[key] for x in out])
    return result


class Simulator:
    """Handles simulations."""
    def on_before_forward(self, sample):
        """Apply transformations to conditions."""
        return sample

    @abstractmethod
    def forward(self, trace):
        """Main function to overwrite.
        """
        raise NotImplementedError

    def on_after_forward(self, sample):
        """Apply transformation to generated samples."""
        return sample

    def _run(self, targets = None, conditions = {}, log_prob = False):
        conditions = conditions() if callable(conditions) else conditions

        conditions = self.on_before_forward(conditions)
        trace = Trace(targets, conditions, log_prob)
        if not trace.covers_targets:
            self.forward(trace)
            #try:
            #    self.forward(trace)
            #except CoversTargetException:
            #    pass
        if targets is not None and not trace.covers_targets:
            raise ValueError("Missing simulation targets.")
        result = self.on_after_forward(dict(trace))

        return result
    
    def get_shapes_and_dtypes(self, targets = None):
        """Return shapes and data-types of sample variables.

        Args:
            targets: Target sample variables to simulate.

        Return:
            dictionary of shapes, dictionary of dtypes
        """
        sample = self(targets = targets)
        shapes = {k: tuple(v.shape) for k, v in sample.items()}
        dtypes = {k: v.dtype for k, v in sample.items()}
        return shapes, dtypes

    def __call__(self, N = None, targets = None, conditions = {}, log_prob = False):
        """Sample from the simulator.

        Args:
            N: int, number of samples to generate
            targets: Optional list of target sample variables to generate. If `None`, all targets are simulated.
            conditions: Dict or Callable, conditions sample variables.
        """
        if N is None:
            return self._run(targets, conditions, log_prob = log_prob)

        out = []
        for _ in tqdm(range(N)):
            result = self._run(targets, conditions, log_prob = log_prob)
            out.append(result)
        out = collate_output(out)
        out = Samples(out)
        return out

    def get_resampler(self, targets):
        """Generates a resampler. Useful for noise hooks etc.

        Args:
            targets: List of target variables to simulate

        Returns:
            SimulatorResampler instance.
        """
        return SimulatorResampler(self, targets)

    def get_iterator(self, targets = None, conditions = {}):
        """Generates an iterator. Useful for iterative sampling.

        Args:
            targets: Optional list of target sample variables.
            conditions: Dict or Callable.
        """
        def iterator():
            while True:
                yield self._run(targets = targets, conditions = conditions)

        return iterator
    
    
class SimulatorResampler:
    """Handles rerunning part of the simulator. Typically used for on-the-fly calculations during training."""
    def __init__(self, simulator, targets):
        """Instantiates SimulatorResampler

        Args:
            simulator: The simulator object
            targets: List of target sample variables that will be resampled
        """
        self._simulator = simulator
        self._targets = targets
        
    def __call__(self, sample):
        """Resamples.

        Args:
            sample: Sample dict

        Returns:
            sample with resampled sites
        """
        conditions = sample.copy()
        for k in self._targets:
            conditions.pop(k)
        sims = self._simulator(conditions = conditions, targets = self._targets)
        return sims


#############
# SwyftModule
#############

class SwyftModule(pl.LightningModule):
    """Handles training of ratio estimators."""
    def __init__(self, lr = 1e-3, lrs_factor = 0.1, lrs_patience = 5):
        """Instantiates SwyftModule.

        Args:
            lr: learning rate
            lrs_factor: learning rate decay
            lrs_patience: learning rate decay patience
        """
        super().__init__()
        self.save_hyperparameters()
        self._predict_condition_x = {}
        self._predict_condition_z = {}

    def on_train_start(self):
        self.logger.log_hyperparams(self.hparams, {"hp/KL-div": -1, "hp/JS-div": -1})
        
    def on_train_end(self):
        for cb in self.trainer.callbacks:
            if isinstance(cb, pl.callbacks.model_checkpoint.ModelCheckpoint):
                cb.to_yaml()
      
    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), self.hparams.lr)
        lr_scheduler = {"scheduler": torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, factor=self.hparams.lrs_factor, patience=self.hparams.lrs_patience), "monitor": "val_loss"}
        return dict(optimizer = optimizer, lr_scheduler = lr_scheduler)

    def _log_ratios(self, x, z):
        out = self(x, z)
        out = {k: v for k, v in out.items() if k[:4] != 'aux_'}
        log_ratios = torch.cat([val.ratios.flatten(start_dim = 1) for val in out.values()], dim=1)
        return log_ratios
    
    def validation_step(self, batch, batch_idx):
        loss = self._calc_loss(batch, randomized = False)
        self.log("val_loss", loss, prog_bar=True)
        return loss

    def _calc_loss(self, batch, randomized = True):
        """Calculate batch-averaged loss summed over ratio estimators.
        """
        if isinstance(batch, list):  # multiple dataloaders provided, using second one for contrastive samples
            A = batch[0]
            B = batch[1]
        else:  # only one dataloader provided, using same samples for constrative samples
            A = batch
            B = valmap(lambda z: torch.roll(z, 1, dims = 0), A)

        # Concatenate positive samples and negative (contrastive) examples
        x = A
        z = {}
        for key in B:
            z[key] = torch.cat([A[key], B[key]])

        num_pos = len(list(x.values())[0])          # Number of positive examples
        num_neg = len(list(z.values())[0])-num_pos  # Number of negative examples

        log_ratios = self._log_ratios(x, z)  # Generates concatenated flattened list of all estimated log ratios
        y = torch.zeros_like(log_ratios)
        y[:num_pos, ...] = 1
        pos_weight = torch.ones_like(log_ratios[0])*num_neg/num_pos
        #loss = F.binary_cross_entropy_with_logits(log_ratios, y, reduction = 'none', pos_weight = pos_weight)
        #print(loss.sum())
        loss = -pos_weight*y*torch.log(torch.sigmoid(log_ratios)+1e-10) - (1-y)*torch.log(torch.sigmoid(-log_ratios)+1e-10)
        #print(loss.sum())
        #qwerty
        num_ratios = loss.shape[1]
        loss = loss.sum()/num_neg  # Calculates batch-averaged loss
        return loss - 2*np.log(2.)*num_ratios
    
    def _calc_KL(self, batch, batch_idx):
        x = batch
        z = batch
        log_ratios = self._log_ratios(x, z)
        nbatch = len(log_ratios)
        loss = -log_ratios.sum()/nbatch
        return loss
        
    def training_step(self, batch, batch_idx):
        loss = self._calc_loss(batch)
        self.log("train_loss", loss)
        return loss

    def test_step(self, batch, batch_idx):
        loss = self._calc_loss(batch, randomized = False)
        lossKL = self._calc_KL(batch, batch_idx)
        self.log("hp/JS-div", loss)
        #self.log("hp_metric", loss)
        self.log("hp/KL-div", lossKL)
        return loss
    
    def _set_predict_conditions(self, condition_x, condition_z):
        self._predict_condition_x = {k: v.unsqueeze(0) for k, v in condition_x.items()}
        self._predict_condition_z = {k: v.unsqueeze(0) for k, v in condition_z.items()}
        
    def set_conditions(self, conditions):
        self._predict_condition_x = conditions
    
    def predict_step(self, batch, *args, **kwargs):
        A = batch[0]
        B = batch[1]
        return self(A, B)


##############
# SwyftTrainer
##############

class SwyftTrainer(pl.Trainer):
    """Training of SwyftModule, a thin layer around lightning.Trainer."""
    def infer(self, model, A, B, return_sample_ratios = True):
        """Run through model in inference mode.

        Args:
            A: sample or dataloader for samples A.
            B: sample or dataloader for samples B.

        Returns:
            Concatenated network output
        """
        if isinstance(A, dict):
            dl1 = Samples({k: [v] for k, v in A.items()}).get_dataloader(batch_size = 1)
        else:
            dl1 = A
        if isinstance(B, dict):
            dl2 = Samples({k: [v] for k, v in B.items()}).get_dataloader(batch_size = 1)
        else:
            dl2 = B
        dl = CombinedLoader([dl1, dl2], mode = 'max_size_cycle')
        ratio_batches = self.predict(model, dl)
        if return_sample_ratios:
            keys = ratio_batches[0].keys()
            d = {k: Ratios(
                    torch.cat([r[k].values for r in ratio_batches]),
                    torch.cat([r[k].ratios for r in ratio_batches])
                    ) for k in keys if k[:4] != "aux_"
                }
            return SampleRatios(**d)
        else:
            return ratio_batches
    
    def estimate_mass(self, model, A, B, batch_size = 1024):
        """Estimate empirical mass.

        Args:
            model: network
            A: truth samples
            B: prior samples
            batch_size: batch sized used during network evaluation

        Returns:
            Dict of PosteriorMass objects.
        """
        repeat = len(B)//batch_size + (len(B)%batch_size>0)
        pred0 = self.infer(model, A.get_dataloader(batch_size=32), A.get_dataloader(batch_size=32))
        pred1 = self.infer(model, A.get_dataloader(batch_size=1, repeat = repeat), B.get_dataloader(batch_size = batch_size))
        n0 = len(pred0)
        out = {}
        for k, v in pred1.items():
            ratios = v.ratios.reshape(n0, -1, *v.ratios.shape[1:])
            vs = []
            ms = []
            for i in range(n0):
                ratio0 = pred0[k].ratios[i]
                value0 = pred0[k].values[i]
                m = calc_mass(ratio0, ratios[i])
                vs.append(value0)
                ms.append(m)
            masses = torch.stack(ms, dim = 0)
            values = torch.stack(vs, dim = 0)
            out[k] = PosteriorMass(values, masses)
        return out


@dataclass
class PosteriorMass:
    """Handles masses and the corresponding parameter values."""
    values: None
    masses: None

@dataclass
class Ratios:
    """Handles ratios and the corresponding parameter values.
    
    A dictionary of Ratios is expected to be returned by ratio estimation networks.

    Args:
        values: tensor of values for which the ratios were estimated, (nbatch, *shape_ratios, *shape_params)
        ratios: tensor of estimated ratios, (nbatch, *shape_ratios)
    """
    values: torch.Tensor
    ratios: torch.Tensor
    metadata: dict = field(default_factory = dict)
    
    def __len__(self):
        """Number of stored ratios."""
        assert len(self.values) == len(self.ratios), "Inconsistent Ratios"
        return len(self.values)
    
    def weights(self, normalize = False):
        """Calculate weights based on ratios.

        Args:
            normalize: If true, normalize weights to sum to one.  If false, return weights = exp(ratios).
        """
        ratios = self.ratios
        if normalize:
            ratio_max = ratios.max(axis=0).values
            weights = torch.exp(ratios-ratio_max)
            weights_total = weights.sum(axis=0)
            weights = weights/weights_total*len(weights)
        else:
            weights = torch.exp(ratios)
        return weights
    
    def sample(self, N, replacement = True):
        """Subsample values based on normalized weights.

        Args:
            N: Number of samples to generate
            replacement: Sample with replacement.  Default is true, which corresponds to generating samples from the posterior.
        """
        weights = self.weights(normalize = True)
        if not replacement and N > len(self):
            N = len(self)
        samples = weights_sample(N, self.values, weights, replacement = replacement)
        return samples


def calc_mass(r0, r):
    p = torch.exp(r - r.max(axis=0).values)
    p /= p.sum(axis=0)
    m = r > r0
    return (p*m).sum(axis=0)

def weights_sample(N, values, weights, replacement = True):
    """Weight-based sampling with or without replacement."""
    sw = weights.shape
    sv = values.shape
    assert sw == sv[:len(sw)], "Overlapping left-handed weights and values shapes do not match: %s vs %s"%(str(sv), str(sw))
    
    w = weights.view(weights.shape[0], -1)
    idx = torch.multinomial(w.T, N, replacement = replacement).T
    si = tuple(1 for _ in range(len(sv)-len(sw)))
    idx = idx.view(N, *sw[1:], *si)
    idx = idx.expand(N, *sv[1:])
    
    samples = torch.gather(values, 0, idx)
    return samples

def tensorboard_config(save_dir = "./lightning_logs", name = None, version = None, patience = 3):
    """Generates convenience configuration for Trainer object.

    Args:
        save_dir: Save-directory for tensorboard logs
        name: tensorboard logs name
        version: tensorboard logs version
        patience: early-stopping patience

    Returns:
        Configuration dictionary
    """
    tbl = pl_loggers.TensorBoardLogger(save_dir = save_dir, name = name, version = version, default_hp_metric = False)
    lr_monitor = LearningRateMonitor(logging_interval="step")
    early_stop_callback = EarlyStopping(monitor="val_loss", min_delta=0.0, patience=patience, verbose=False, mode="min")
    checkpoint_callback = ModelCheckpoint(monitor="val_loss")
    return dict(logger = tbl, callbacks = [lr_monitor, early_stop_callback, checkpoint_callback])

def get_best_model(tbl):
    """Get best model from tensorboard log. Useful for reloading trained networks.

    Args:
        tbl: Tensorboard log instance

    Returns:
        path to best model
    """
    try:
        with open(tbl.experiment.get_logdir()+"/checkpoints/best_k_models.yaml") as f:
            best_k_models = yaml.load(f, Loader = yaml.FullLoader)    
    except FileNotFoundError:
        return None
    val_loss = np.inf
    path = None
    for k, v in best_k_models.items():
        if v < val_loss:
            path = k
            val_loss = v
    return path


########
# Bounds
########

@dataclass
class MeanStd:
    """Store mean and standard deviation"""
    mean: torch.Tensor
    std: torch.Tensor

    def from_samples(samples, weights = None):
        """
        Estimate mean and std deviation of samples by averaging over first dimension.
        Supports weights>=0 with weights.shape = samples.shape
        """
        if weights is None:
            weights = torch.ones_like(samples)
        mean = (samples*weights).sum(axis=0)/weights.sum(axis=0)
        res = samples - mean
        var = (res*res*weights).sum(axis=0)/weights.sum(axis=0)
        return MeanStd(mean = mean, std = var**0.5)


@dataclass
class RectangleBound:
    low: torch.Tensor
    high: torch.Tensor


def get_1d_rect_bounds(samples, th = 1e-6):
    bounds = {}
    for k, v in samples.items():
        r = v.ratios
        r = r - r.max(axis=0).values  # subtract peak
        p = v.values
        #w = v[..., 0]
        #p = v[..., 1]
        all_max = p.max(dim=0).values
        all_min = p.min(dim=0).values
        constr_min = torch.where(r > np.log(th), p, all_max).min(dim=0).values
        constr_max = torch.where(r > np.log(th), p, all_min).max(dim=0).values
        #bound = torch.stack([constr_min, constr_max], dim = -1)
        bound = RectangleBound(constr_min, constr_max)
        bounds[k] = bound
    return bounds


########
# Stores
########

class Samples(dict):
    """Handles storing samples in memory.  Samples are stored as dictionary of arrays/tensors with num of samples as first dimension."""
    def __len__(self):
        n = [len(v) for v in self.values()] 
        assert all([x == n[0] for x in n]), "Inconsistent lengths in Samples"
        return n[0]
    
    def __getitem__(self, i):
        """For integers, return 'rows', for string returns 'columns'."""
        if isinstance(i, int):
            return {k: v[i] for k, v in self.items()}
        elif isinstance(i, slice):
            return Samples({k: v[i] for k, v in self.items()})
        else:
            return super().__getitem__(i)
        
    def get_dataset(self, on_after_load_sample = None):
        """Generator function for SamplesDataset object.

        Args:
            on_after_load_sample: Callable, that is applied to individual samples on the fly.

        Returns:
            SamplesDataset
        """
        return SamplesDataset(self, on_after_load_sample = on_after_load_sample)
    
    def get_dataloader(self, batch_size = 1, shuffle = False, on_after_load_sample = None, repeat = None):
        """Generator function to directly generate a dataloader object.

        Args:
            batch_size: batch_size for dataloader
            shuffle: shuffle for dataloader
            on_after_load_sample: see `get_dataset`
            repeat: If not None, Wrap dataset in RepeatDatasetWrapper
        """
        dataset = self.get_dataset(on_after_load_sample = on_after_load_sample)
        if repeat is not None:
            dataset = RepeatDatasetWrapper(dataset, repeat = repeat)
        return torch.utils.data.DataLoader(dataset, batch_size = batch_size, shuffle = shuffle)
    
    def to_numpy(self, single_precision = True):
        return to_numpy(self, single_precision = single_precision)
        

# TODO: This is the return type of ratio estimation networks. Maybe make use of that somehow?
class SampleRatios(dict):
    """Return type of infer operation of SwyftTrainer"""
    def __len__(self):
        n = [len(v) for v in self.values()]
        assert all([x == n[0] for x in n]), "Inconsistent lengths in Samples"
        return n[0]
    
    def sample(self, N, replacement = True):
        samples = {k: v.sample(N, replacement = replacement) for k, v in self.items()}
        return Samples(samples)


class SamplesDataset(torch.utils.data.Dataset):
    """Simple torch dataset based on Samples."""
    def __init__(self, sample_store, on_after_load_sample = None):
        self._dataset = sample_store
        self._on_after_load_sample = on_after_load_sample

    def __len__(self):
        return len(self._dataset[list(self._dataset.keys())[0]])
    
    def __getitem__(self, i):
        d = {k: v[i] for k, v in self._dataset.items()}
        if self._on_after_load_sample is not None:
            d = self._on_after_load_sample(d)
        return d


class RepeatDatasetWrapper(torch.utils.data.Dataset):
    def __init__(self, dataset, repeat):
        self._dataset = dataset
        self._repeat = repeat

    def __len__(self):
        return len(self._dataset)*self._repeat

    def __getitem__(self, i):
        return self._dataset[i//self._repeat]
    

##################
# Helper functions
##################

    
#def append_randomized(z):
#    # Append randomized samples, e.g.: 1, 2, 3, 4 -> 1, 2, 3, 4, 2, 4, 3, 1
#    assert len(z)%2 == 0, "Cannot expand odd batch dimensions."
#    n = len(z)//2
#    idx = torch.randperm(n)
#    z = torch.cat([z, z[n+idx], z[idx]])
#    return z

#def randomize(z):
#    idx = torch.randperm(len(z))
#    return z[idx]

#def roll(z):
#    return torch.roll(z, 1, dims = 0)

#def append_nonrandomized(z):
#    # Append swapped samples: z1, z2, z3, z4 --> z1, z2, z3, z4, z3, z4, z1, z2
#    assert len(z)%2 == 0, "Cannot expand odd batch dimensions."
#    n = len(z)//2
#    idx = np.arange(n)
#    z = torch.cat([z, z[n+idx], z[idx]])
#    return z

## https://stackoverflow.com/questions/16463582/memoize-to-disk-python-persistent-memoization
##def persist_to_file():
#def persist_to_file(original_func):
#        def new_func(*args, file_path = None, **kwargs):
#            cache = None
#            if file_path is not None:
#                try:
#                    cache = torch.load(file_path)
#                except (FileNotFoundError, ValueError):
#                    pass
#            if cache is None:
#                cache = original_func(*args, **kwargs)
#                if file_path is not None:
#                    torch.save(cache, file_path)
#            return cache
#        return new_func
#    #return decorator
#    
#def file_cache(fn, file_path):
#    try:
#        cache = torch.load(file_path)
#    except (FileNotFoundError, ValueError):
#        cache = None
#    if cache is None:
#        cache = fn()
#        torch.save(cache, file_path)
#    return cache
    
## RENAME?
#def dictstoremap(model, dictstore):
#    """Generate new dictionary."""
#    N = len(dictstore)
#    out = []
#    for i in tqdm(range(N)):
#        x = model(dictstore[i])
#        out.append(x)
#    out = torch.utils.data.dataloader.default_collate(out) # using torch internal functionality for this, yay!
#    out = {k: v.cpu() for k, v in out.items()}
#    return Samples(out)

    
    
##########################
# Ratio estimator networks
##########################

def equalize_tensors(a, b):
    n, m = len(a), len(b)
    if n == m:
        return a, b
    elif n == 1:
        shape = list(a.shape)
        shape[0] = m
        return a.expand(*shape), b
    elif m == 1:
        shape = list(b.shape)
        shape[0] = n
        return a, b.expand(*shape)
    elif n < m:
        assert m%n == 0, "Cannot equalize tensors with non-divisible batch sizes."
        shape = [1 for _ in range(a.dim())]
        shape[0] = m//n
        return a.repeat(*shape), b
    else:
        assert n%m == 0, "Cannot equalize tensors with non-divisible batch sizes."
        shape = [1 for _ in range(b.dim())]
        shape[0] = n//m
        return a, b.repeat(*shape)
    
# TODO: Introduce RatioEstimatorDense
class RatioEstimatorMLPnd(torch.nn.Module):
    def __init__(self, x_dim, marginals, dropout = 0.1, hidden_features = 64, num_blocks = 2):
        super().__init__()
        self.marginals = marginals
        self.ptrans = swyft.networks.ParameterTransform(
            len(marginals), marginals, online_z_score=False
        )
        n_marginals, n_block_parameters = self.ptrans.marginal_block_shape
        n_observation_features = x_dim
        self.classifier = swyft.networks.MarginalClassifier(
            n_marginals,
            n_observation_features + n_block_parameters,
            hidden_features=hidden_features,
            dropout_probability = dropout,
            num_blocks=num_blocks,
        )
        
    def forward(self, x, z):
        x, z = equalize_tensors(x, z)
        z = self.ptrans(z)
        ratios = self.classifier(x, z)
        w = Ratios(z, ratios, metadata = {"type": "MarginalMLP", "marginals": self.marginals})
        return w
    

class RatioEstimatorMLP1d(torch.nn.Module):
    def __init__(self, x_dim, z_dim, dropout = 0.1, hidden_features = 64, num_blocks = 2):
        super().__init__()
        self.marginals = [(i,) for i in range(z_dim)]
        self.ptrans = swyft.networks.ParameterTransform(
            len(self.marginals), self.marginals, online_z_score=True
        )
        n_marginals, n_block_parameters = self.ptrans.marginal_block_shape
        n_observation_features = x_dim
        self.classifier = swyft.networks.MarginalClassifier(
            n_marginals,
            n_observation_features + n_block_parameters,
            hidden_features=hidden_features,
            dropout_probability = dropout,
            num_blocks=num_blocks,
        )
        
    def forward(self, x, z):
        x, z = equalize_tensors(x, z)
        zt = self.ptrans(z).detach()
        ratios = self.classifier(x, zt)
        w = Ratios(z, ratios, metadata = {"type": "MLP1d"})
        return w


class RatioEstimatorGaussian1d(torch.nn.Module):
    def __init__(self, momentum = 0.1):
        super().__init__()
        self.momentum = momentum        
        self.x_mean = None
        self.z_mean = None
        self.x_var = None
        self.z_var = None
        self.xz_cov = None
        
    def forward(self, x: torch.Tensor, z: torch.Tensor) -> torch.Tensor:
        """2-dim Gaussian approximation to marginals and joint, assuming (B, N)."""
        print("Warning: deprecated, might be broken")
        x, z = equalize_tensors(x, z)
        if self.training or self.x_mean is None:
            # Covariance estimates must be based on joined samples only
            # NOTE: This makes assumptions about the structure of mini batches during training (J, M, M, J, J, M, M, J, ...)
            # TODO: Change to (J, M, J, M, J, M, ...) in the future
            batch_size = len(x)
            #idx = np.array([[i, i+3] for i in np.arange(0, batch_size, 4)]).flatten() 
            idx = np.arange(batch_size//2)  # TODO: Assuming (J, J, J, J, M, M, M, M) etc
            
            # Estimation w/o Bessel's correction, using simple MLE estimate (https://en.wikipedia.org/wiki/Estimation_of_covariance_matrices)
            x_mean_batch = x[idx].mean(dim=0).detach()
            z_mean_batch = z[idx].mean(dim=0).detach()
            x_var_batch = ((x[idx]-x_mean_batch)**2).mean(dim=0).detach()
            z_var_batch = ((z[idx]-z_mean_batch)**2).mean(dim=0).detach()
            xz_cov_batch = ((x[idx]-x_mean_batch)*(z[idx]-z_mean_batch)).mean(dim=0).detach()
            
            # Momentum-based update rule
            momentum = self.momentum
            self.x_mean = x_mean_batch if self.x_mean is None else (1-momentum)*self.x_mean + momentum*x_mean_batch
            self.x_var = x_var_batch if self.x_var is None else (1-momentum)*self.x_var + momentum*x_var_batch
            self.z_mean = z_mean_batch if self.z_mean is None else (1-momentum)*self.z_mean + momentum*z_mean_batch
            self.z_var = z_var_batch if self.z_var is None else (1-momentum)*self.z_var + momentum*z_var_batch
            self.xz_cov = xz_cov_batch if self.xz_cov is None else (1-momentum)*self.xz_cov + momentum*xz_cov_batch
            
        # log r(x, z) = log p(x, z)/p(x)/p(z), with covariance given by [[x_var, xz_cov], [xz_cov, z_var]]
        xb = (x-self.x_mean)/self.x_var**0.5
        zb = (z-self.z_mean)/self.z_var**0.5
        rho = self.xz_cov/self.x_var**0.5/self.z_var**0.5
        r = -0.5*torch.log(1-rho**2) + rho/(1-rho**2)*xb*zb - 0.5*rho**2/(1-rho**2)*(xb**2 + zb**2)
        #out = torch.cat([r.unsqueeze(-1), z.unsqueeze(-1).detach()], dim=-1)
        out = Ratios(z, r, metadata = {"type": "Gaussian1d"})
        return out


###########
# Obsolete?
###########

#class SimpleDataset(torch.utils.data.Dataset):
#    def __init__(self, **kwargs):
#        self._data = kwargs
#    
#    def __len__(self):
#        k = list(self._data.keys())[0]
#        return len(self._data[k])
#    
#    def __getitem__(self, i):
#        obs = {k: v[i] for k, v in self._data.items()}
#        v = u = self._data['v'][i]
#        return (obs, v, u)


#def subsample_posterior(N, z, replacement = True):
#    # Supports only 1-dim posteriors so far
#    shape = z.shape
#    z = z.view(shape[0], -1, shape[-1])
#    w = z[..., 0]
#    p = z[..., 1]
#    wm = w.max(axis=0).values
#    w = torch.exp(w-wm)
#    idx = torch.multinomial(w.T, N, replacement = replacement).T
#    samples = torch.gather(p, 0, idx)
#    samples = samples.view(N, *shape[1:-1])
#    return samples


#class MultiplyDataset(torch.utils.data.Dataset):
#    def __init__(self, dataset, M):
#        self.dataset = dataset
#        self.M = M
#        
#    def __len__(self):
#        return len(self.dataset)*self.M
#    
#    def __getitem__(self, i):
#        return self.dataset[i%self.M]
        


###################
# Zarr-based Stores
###################


def get_index_slices(idx):
    """Returns list of enumerated consecutive indices"""
    idx = np.array(idx)
    pointer = 0
    residual_idx = idx
    slices = []
    while len(residual_idx) > 0:
        mask = (residual_idx - residual_idx[0] - np.arange(len(residual_idx)) == 0)
        slc1 = [residual_idx[mask][0], residual_idx[mask][-1]+1]
        slc2 = [pointer, pointer+sum(mask)]
        pointer += sum(mask)
        slices.append([slc2, slc1])
        residual_idx = residual_idx[~mask]
    return slices

# TODO: Deprecate
class SwyftDataModule(pl.LightningDataModule):
    def __init__(self, on_after_load_sample = None, store = None, batch_size: int = 32, validation_percentage = 0.2, manual_seed = None, train_multiply = 10 , num_workers = 0):
        super().__init__()
        self.store = store
        self.on_after_load_sample = on_after_load_sample
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.validation_percentage = validation_percentage
        self.train_multiply = train_multiply
        print("Deprecation warning: Use dataloaders directly rathe than this data module for transparency.")

    def setup(self, stage):
        self.dataset = SamplesDataset(self.store, on_after_load_sample= self.on_after_load_sample)#, x_keys = ['data'], z_keys=['z'])
        n_train, n_valid = get_ntrain_nvalid(self.validation_percentage, len(self.dataset))
        self.dataset_train, self.dataset_valid = random_split(self.dataset, [n_train, n_valid], generator=torch.Generator().manual_seed(42))
        self.dataset_test = SamplesDataset(self.store)#, x_keys = ['data'], z_keys=['z'])

    def train_dataloader(self):
        return torch.utils.data.DataLoader(self.dataset_train, batch_size=self.batch_size, num_workers = self.num_workers)

    def val_dataloader(self):
        return torch.utils.data.DataLoader(self.dataset_valid, batch_size=self.batch_size, num_workers = self.num_workers)
    
    # # TODO: Deprecate
    # def predict_dataloader(self):
    #     return torch.utils.data.DataLoader(self.dataset, batch_size=self.batch_size, num_workers = self.num_workers)
    
    def test_dataloader(self):
        return torch.utils.data.DataLoader(self.dataset_test, batch_size=self.batch_size, num_workers = self.num_workers)

    def samples(self, N, random = False):
        dataloader = torch.utils.data.DataLoader(self.dataset_train, batch_size=N, num_workers = 0, shuffle = random)
        examples = next(iter(dataloader))
        return Samples(examples)


class ZarrStore:
    def __init__(self, file_path, sync_path = None):
        if sync_path is None:
            sync_path = file_path + ".sync"
        synchronizer = zarr.ProcessSynchronizer(sync_path) if sync_path else None
        self.store = zarr.DirectoryStore(file_path)
        self.root = zarr.group(store = self.store, synchronizer = synchronizer)
        self.lock = fasteners.InterProcessLock(file_path+".lock.file")
            
    def set_length(self, N, clubber = False):
        """Resize store.  N >= current store length."""
        if N < len(self) and not clubber:
            raise ValueError(
                """New length shorter than current store length.
                You can use clubber = True if you know what your are doing."""
                )
        for k in self.data.keys():
            shape = self.data[k].shape
            self.data[k].resize(N, *shape[1:])
        self.root['meta/sim_status'].resize(N,)
        
    def init(self, N, chunk_size, shapes = None, dtypes = None):
        if len(self) > 0:
            print("WARNING: Already initialized.")
            return self
        self._init_shapes(shapes, dtypes, N, chunk_size)
        return self
        
    def __len__(self):
        if 'data' not in self.root.keys():
            return 0
        keys = self.root['data'].keys()
        ns = [len(self.root['data'][k]) for k in keys]
        N = ns[0]
        assert all([n==N for n in ns])
        return N

    def keys(self):
        return list(self.data.keys())

    def __getitem__(self, i):
        if isinstance(i, int):
            return {k: self.data[k][i] for k in self.keys()}
        elif isinstance(i, slice):
            return Samples({k: self.data[k][i] for k in self.keys()})
        elif isinstance(i, str):
            return self.data[i]
        else:
            raise ValueError
    
    # TODO: Remove consistency checks
    def _init_shapes(self, shapes, dtypes, N, chunk_size):          
        """Initializes shapes, or checks consistency."""
        for k in shapes.keys():
            s = shapes[k]
            dtype = dtypes[k]
            try:
                self.root.zeros('data/'+k, shape = (N, *s), chunks = (chunk_size, *s), dtype = dtype)
            except zarr.errors.ContainsArrayError:
                assert self.root['data/'+k].shape == (N, *s), "Inconsistent array sizes"
                assert self.root['data/'+k].chunks == (chunk_size, *s), "Inconsistent chunk sizes"
                assert self.root['data/'+k].dtype == dtype, "Inconsistent dtype"
        try:
            self.root.zeros('meta/sim_status', shape = (N, ), chunks = (chunk_size, ), dtype = 'i4')
        except zarr.errors.ContainsArrayError:
            assert self.root['meta/sim_status'].shape == (N, ), "Inconsistent array sizes"
        try:
            assert self.chunk_size == chunk_size, "Inconsistent chunk size"
        except KeyError:
            self.data.attrs['chunk_size'] = chunk_size

    @property
    def chunk_size(self):
        return self.data.attrs['chunk_size']

    @property
    def data(self):
        return self.root['data']
    
    def numpy(self):
        return {k: v[:] for k, v in self.root['data'].items()}
    
    def get_sample_store(self):
        return Samples(self.numpy())
    
    @property
    def meta(self):
        return {k: v for k, v in self.root['meta'].items()}
    
    @property
    def sims_required(self):
        return sum(self.root['meta']['sim_status'][:] == 0)

    def simulate(self, sample_fn, max_sims = None, batch_size = 10):
        total_sims = 0
        while self.sims_required > 0:
            if max_sims is not None and total_sims >= max_sims:
                break
            num_sims = self._simulate_batch(sample_fn, batch_size)
            total_sims += num_sims

    def _simulate_batch(self, sample_fn, batch_size):
        # Run simulator
        num_sims = min(batch_size, self.sims_required)
        if num_sims == 0:
            return num_sims

        samples = sample_fn(num_sims)
        
        # Reserve slots
        with self.lock:
            sim_status = self.root['meta']['sim_status']
            data = self.root['data']
            
            idx = np.arange(len(sim_status))[sim_status[:]==0][:num_sims]
            index_slices = get_index_slices(idx)
            
            for i_slice, j_slice in index_slices:
                sim_status[j_slice[0]:j_slice[1]] = 1
                for k, v in data.items():
                    data[k][j_slice[0]:j_slice[1]] = samples[k][i_slice[0]:i_slice[1]]
                
        return num_sims

    def get_dataset(self, idx_range = None, on_after_load_sample = None):
        return ZarrStoreIterableDataset(self, idx_range = idx_range, on_after_load_sample = on_after_load_sample)
    
    def get_dataloader(self, num_workers = 0, batch_size = 1, pin_memory = False, drop_last = True, idx_range = None, on_after_load_sample = None):
        ds = self.get_dataset(idx_range = idx_range, on_after_load_sample = on_after_load_sample)
        dl = torch.utils.data.DataLoader(ds, num_workers = num_workers, batch_size = batch_size, drop_last = drop_last, pin_memory = pin_memory)
        return dl


class ZarrStoreIterableDataset(torch.utils.data.dataloader.IterableDataset):
    def __init__(self, zarr_store : ZarrStore, idx_range = None, on_after_load_sample = None):
        self.zs = zarr_store
        if idx_range is None:
            self.n_samples = len(self.zs)
            self.offset = 0
        else:
            self.offset = idx_range[0]
            self.n_samples = idx_range[1] - idx_range[0]
        self.chunk_size = self.zs.chunk_size
        self.n_chunks = int(math.ceil(self.n_samples/float(self.chunk_size)))
        self.on_after_load_sample = on_after_load_sample
      
    @staticmethod
    def get_idx(n_chunks, worker_info):
        if worker_info is not None:
            num_workers = worker_info.num_workers
            worker_id = worker_info.id
            n_chunks_per_worker = int(math.ceil(n_chunks/float(num_workers)))
            idx = [worker_id*n_chunks_per_worker, min((worker_id+1)*n_chunks_per_worker, n_chunks)]
            idx = np.random.permutation(range(*idx))
        else:
            idx = np.random.permutation(n_chunks)
        return idx
    
    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        idx = self.get_idx(self.n_chunks, worker_info)
        offset = self.offset
        for i0 in idx:
            # Read in chunks
            data_chunk = {}
            for k in self.zs.data.keys():
                data_chunk[k] = self.zs.data[k][offset+i0*self.chunk_size:offset+(i0+1)*self.chunk_size]
            n = len(data_chunk[k])
                
            # Return separate samples
            for i in np.random.permutation(n):
                out = {k: v[i] for k, v in data_chunk.items()}
                if self.on_after_load_sample:
                    out = self.on_after_load_sample(out)
                yield out

