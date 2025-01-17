from NanoParticleTools.inputs.nanoparticle import SphericalConstraint
from NanoParticleTools.machine_learning.util.learning_rate import ReduceLROnPlateauWithWarmup
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks import (LearningRateMonitor,
                                         StochasticWeightAveraging,
                                         ModelCheckpoint)
from pytorch_lightning.loggers import WandbLogger

import wandb
import pytorch_lightning as pl
import os
from ray.tune.schedulers import ASHAScheduler

from NanoParticleTools.util.visualization import plot_nanoparticle

from ray.tune.integration.pytorch_lightning import TuneReportCallback
from matplotlib import pyplot as plt
import numpy as np
import torch
from pandas import DataFrame
from matplotlib import ticker as mticker
from matplotlib.lines import Line2D

from collections.abc import Callable


def train_spectrum_model(config: dict,
                         model_cls: pl.LightningModule,
                         data_module: pl.LightningDataModule,
                         lr_scheduler: torch.optim.lr_scheduler.
                         _LRScheduler = ReduceLROnPlateauWithWarmup,
                         lr_scheduler_kwargs: dict | None = None,
                         num_epochs: int = 2000,
                         ray_tune: bool = False,
                         early_stop: bool = False,
                         swa: bool = False,
                         save_checkpoints: bool = True,
                         wandb_config: dict | None = None,
                         trainer_device_config: dict | None = None,
                         additional_callbacks: list | None = None):
    """
        params
        model_cls:
        model_config:
        lr_scheduler:
        augment_loss:
        ray_tune: whether or not this is a ray tune run
        early_stop: whether or not to use early stopping
        swa: whether or not to use stochastic weight averaging

        """
    if lr_scheduler_kwargs is None:
        lr_scheduler_kwargs = {
            'warmup_epochs': 10,
            'patience': 100,
            'factor': 0.8
        }

    if trainer_device_config is None:
        trainer_device_config = {'accelerator': 'auto'}

    if wandb_config is None:
        wandb_config = {'name': None}

    # Make the model
    model = model_cls(lr_scheduler=lr_scheduler,
                      lr_scheduler_kwargs=lr_scheduler_kwargs,
                      optimizer_type='adam',
                      **config)

    # Make WandB logger
    wandb_logger = WandbLogger(log_model=True, **wandb_config)

    # Configure callbacks
    callbacks = []
    callbacks.append(LearningRateMonitor(logging_interval='step'))

    # Disable augment loss for now. It seems to not help
    # if augment_loss:
    #     callbacks.append(LossAugmentCallback(aug_loss_epoch=augment_loss))
    if early_stop:
        callbacks.append(EarlyStopping(monitor='val_loss', patience=200))
    if swa:
        callbacks.append(StochasticWeightAveraging(swa_lrs=1e-3))
    if ray_tune:
        callbacks.append(
            TuneReportCallback({"loss": "val_loss"}, on="validation_end"))
    if save_checkpoints:
        checkpoint_callback = ModelCheckpoint(save_top_k=1,
                                              monitor="val_loss",
                                              save_last=True)
        callbacks.append(checkpoint_callback)

    if additional_callbacks is not None:
        # Allow for custom callbacks to be passed in
        callbacks.extend(additional_callbacks)

    # Make the trainer
    trainer = pl.Trainer(max_epochs=num_epochs,
                         enable_progress_bar=False,
                         logger=wandb_logger,
                         callbacks=callbacks,
                         **trainer_device_config)

    trainer.fit(model=model, datamodule=data_module.cuda())

    # Load the best model checkpoint, set it to evaluation mode, and then evaluate the metrics
    # for the training, validation, and test sets
    model = model_cls.load_from_checkpoint(checkpoint_callback.best_model_path)
    model.cuda()
    model.eval()

    # Train metrics
    train_metrics = {}
    factor = 1 / len(data_module.train_dataloader())
    for batch_idx, batch in enumerate(data_module.train_dataloader()):
        _, _loss_d = model._step('train_eval', batch, batch_idx, log=False)
        for key in _loss_d:
            try:
                train_metrics[key] += _loss_d[key].item() * factor
            except KeyError:
                train_metrics[key] = _loss_d[key] * factor
    wandb_logger.log_metrics(train_metrics)

    # Validation metrics
    trainer.validate(dataloaders=data_module.val_dataloader(),
                     ckpt_path='best')
    # Test metrics
    trainer.test(dataloaders=data_module.test_dataloader(), ckpt_path='best')

    # Get sample nanoparticle predictions within the test set
    columns = [
        'nanoparticle', 'spectrum', 'zoomed_spectrum', 'loss', 'npmc_qy',
        'pred_qy'
    ]
    save_data = []
    rng = np.random.default_rng(seed=10)
    for i in rng.choice(range(len(data_module.npmc_test)), 20, replace=False):
        data = data_module.npmc_test[i]
        save_data.append(get_logged_data(trainer.model, data))

    wandb_logger.log_table(key='sample_table', columns=columns, data=save_data)

    # Log the additional data
    log_additional_data(model, data_module, wandb_logger)
    wandb.finish()

    return model


def train_uv_model(config: dict,
                   model_cls: pl.LightningModule,
                   data_module: pl.LightningDataModule,
                   lr_scheduler: torch.optim.lr_scheduler.
                   _LRScheduler = ReduceLROnPlateauWithWarmup,
                   lr_scheduler_kwargs: dict = None,
                   initial_model: pl.LightningModule = None,
                   num_epochs: int = 2000,
                   ray_tune: bool = False,
                   early_stop: bool = False,
                   early_stop_patience: int = 200,
                   swa: bool = False,
                   save_checkpoints: bool = True,
                   wandb_config: dict | None = None,
                   trainer_device_config: dict | None = None,
                   additional_callbacks: list | None = None):
    """
        params
        model_cls:
        model_config:
        lr_scheduler:
        augment_loss:
        ray_tune: whether or not this is a ray tune run
        early_stop: whether or not to use early stopping
        swa: whether or not to use stochastic weight averaging

    """
    if lr_scheduler_kwargs is None:
        lr_scheduler_kwargs = {
            'warmup_epochs': 10,
            'patience': 100,
            'factor': 0.8
        }

    if trainer_device_config is None:
        trainer_device_config = {'accelerator': 'auto'}

    if wandb_config is None:
        wandb_config = {'name': None}

    # Make the model
    if initial_model is None:
        model = model_cls(lr_scheduler=lr_scheduler,
                          lr_scheduler_kwargs=lr_scheduler_kwargs,
                          optimizer_type='adam',
                          **config)
    else:
        model = initial_model
    # Make WandB logger
    wandb_logger = WandbLogger(log_model=True, **wandb_config)

    # Configure callbacks
    callbacks = []
    callbacks.append(LearningRateMonitor(logging_interval='step'))

    # Disable augment loss for now. It seems to not help
    # if augment_loss:
    #     callbacks.append(LossAugmentCallback(aug_loss_epoch=augment_loss))
    if early_stop:
        callbacks.append(
            EarlyStopping(monitor='val_loss', patience=early_stop_patience))
    if swa:
        callbacks.append(StochasticWeightAveraging(swa_lrs=1e-3))
    if ray_tune:
        callbacks.append(
            TuneReportCallback({"loss": "val_loss"}, on="validation_end"))
    if save_checkpoints:
        checkpoint_callback = ModelCheckpoint(save_top_k=1,
                                              monitor="val_loss",
                                              save_last=True)
        callbacks.append(checkpoint_callback)

    if additional_callbacks is not None:
        # Allow for custom callbacks to be passed in
        callbacks.extend(additional_callbacks)

    # Make the trainer
    trainer = pl.Trainer(max_epochs=num_epochs,
                         enable_progress_bar=False,
                         logger=wandb_logger,
                         callbacks=callbacks,
                         **trainer_device_config)

    try:
        trainer.fit(model=model, datamodule=data_module)
    except Exception as e:
        if isinstance(e, KeyboardInterrupt):
            # If keyboard interupt, we'll let the statistics be logged
            pass
        else:
            # If there's any other exception, we'll let it raise
            raise e

    # Load the best model checkpoint, set it to evaluation mode, and then evaluate the metrics
    # for the training, validation, and test sets
    model = model_cls.load_from_checkpoint(checkpoint_callback.best_model_path)
    model.eval()

    # Train metrics
    train_metrics = {}
    factor = 1 / len(data_module.train_dataloader())
    for batch_idx, batch in enumerate(data_module.train_dataloader()):
        _, _loss_d = model._step('train_eval', batch, batch_idx, log=False)
        for key in _loss_d:
            try:
                train_metrics[key] += _loss_d[key].item() * factor
            except KeyError:
                train_metrics[key] = _loss_d[key] * factor
    wandb_logger.log_metrics(train_metrics)

    # Validation metrics
    if data_module.val_dataset is not None:
        val_metrics = {}
        for batch_idx, batch in enumerate(data_module.val_dataloader()):
            _, _loss_d = model._step('val', batch, batch_idx, log=False)
            for key in _loss_d:
                try:
                    val_metrics[key] += _loss_d[key].item() * data_module.batch_size
                except KeyError:
                    val_metrics[key] = _loss_d[key] * data_module.batch_size

        # For the testing set, batches may not be all the same size due to drop_last=False,
        # so we need to account for that.
        for key in val_metrics:
            val_metrics[key] /= len(data_module.val_dataset)
        wandb_logger.log_metrics(val_metrics)

    # If a OOD test set is specified in the data module, we'll obtain the metrics for that as well
    if data_module.test_dataset is not None:
        ood_test_metrics = {}
        for batch_idx, batch in enumerate(data_module.test_dataloader()):
            _, _loss_d = model._step('test', batch, batch_idx, log=False)
            for key in _loss_d:
                try:
                    ood_test_metrics[key] += _loss_d[key].item() * data_module.batch_size
                except KeyError:
                    ood_test_metrics[key] = _loss_d[key] * data_module.batch_size

        # For the testing set, batches may not be all the same size due to drop_last=False,
        # so we need to account for that.
        for key in ood_test_metrics:
            ood_test_metrics[key] /= len(data_module.test_dataset)
        wandb_logger.log_metrics(ood_test_metrics)

    # If a IID test set is specified in the data module, we'll obtain the metrics for that as well
    if data_module.iid_test_dataset is not None:
        iid_test_metrics = {}
        for batch_idx, batch in enumerate(data_module.iid_test_dataloader()):
            _, _loss_d = model._step('iid_test', batch, batch_idx, log=False)
            for key in _loss_d:
                try:
                    iid_test_metrics[key] += _loss_d[key].item(
                    ) * data_module.batch_size
                except KeyError:
                    iid_test_metrics[
                        key] = _loss_d[key] * data_module.batch_size

        # For the testing set, batches may not be all the same size due to drop_last=False,
        # so we need to account for that.
        for key in iid_test_metrics:
            iid_test_metrics[key] /= len(data_module.iid_test_dataset)
        wandb_logger.log_metrics(iid_test_metrics)

    wandb.finish()

    return model


def train_uv_model_augment(config: dict,
                           model_cls: pl.LightningModule,
                           data_module: pl.LightningDataModule,
                           lr_scheduler: torch.optim.lr_scheduler.
                           _LRScheduler = ReduceLROnPlateauWithWarmup,
                           lr_scheduler_kwargs: dict = None,
                           initial_model_path: str | None = None,
                           num_epochs: int = 2000,
                           ray_tune: bool = False,
                           early_stop: bool = False,
                           early_stop_patience: int = 200,
                           swa: bool = False,
                           save_checkpoints: bool = True,
                           wandb_config: dict | None = None,
                           trainer_device_config: dict | None = None,
                           additional_callbacks: list | None = None):
    """
        params
        model_cls:
        model_config:
        lr_scheduler:
        augment_loss:
        ray_tune: whether or not this is a ray tune run
        early_stop: whether or not to use early stopping
        swa: whether or not to use stochastic weight averaging

    """
    if lr_scheduler_kwargs is None:
        lr_scheduler_kwargs = {
            'warmup_epochs': 10,
            'patience': 100,
            'factor': 0.8
        }

    if trainer_device_config is None:
        trainer_device_config = {'accelerator': 'auto'}

    if wandb_config is None:
        wandb_config = {'name': None}

    # Make the model
    if initial_model_path is None:
        model = model_cls(lr_scheduler=lr_scheduler,
                          lr_scheduler_kwargs=lr_scheduler_kwargs,
                          optimizer_type='adam',
                          **config)
    else:
        model = model_cls.load_from_checkpoint(initial_model_path)
    # Make WandB logger
    wandb_logger = WandbLogger(log_model=True, **wandb_config)

    # Configure callbacks
    callbacks = []
    callbacks.append(LearningRateMonitor(logging_interval='step'))

    # Disable augment loss for now. It seems to not help
    # if augment_loss:
    #     callbacks.append(LossAugmentCallback(aug_loss_epoch=augment_loss))
    if early_stop:
        callbacks.append(
            EarlyStopping(monitor='val_loss', patience=early_stop_patience))
    if swa:
        callbacks.append(StochasticWeightAveraging(swa_lrs=1e-3))
    if ray_tune:
        callbacks.append(
            TuneReportCallback({"loss": "val_loss"}, on="validation_end"))
    if save_checkpoints:
        checkpoint_callback = ModelCheckpoint(save_top_k=1,
                                              monitor="val_loss",
                                              save_last=True)
        callbacks.append(checkpoint_callback)

    if additional_callbacks is not None:
        # Allow for custom callbacks to be passed in
        callbacks.extend(additional_callbacks)

    # Make the trainer
    trainer = pl.Trainer(max_epochs=num_epochs,
                         enable_progress_bar=False,
                         logger=wandb_logger,
                         callbacks=callbacks,
                         **trainer_device_config)

    try:
        trainer.fit(model=model, datamodule=data_module)
    except Exception as e:
        if isinstance(e, KeyboardInterrupt):
            # If keyboard interupt, we'll let the statistics be logged
            pass
        else:
            # If there's any other exception, we'll let it raise
            raise e

    # Load the best model checkpoint, set it to evaluation mode, and then evaluate the metrics
    # for the training, validation, and test sets
    model = model_cls.load_from_checkpoint(checkpoint_callback.best_model_path)
    model.eval()

    # Train metrics
    train_metrics = {}
    factor = 1 / len(data_module.train_dataloader())
    for batch_idx, batch in enumerate(data_module.train_dataloader()):
        _, _loss_d = model._step('train_eval', batch, batch_idx, log=False)
        for key in _loss_d:
            try:
                train_metrics[key] += _loss_d[key].item() * factor
            except KeyError:
                train_metrics[key] = _loss_d[key] * factor
    wandb_logger.log_metrics(train_metrics)

    # Validation metrics
    if data_module.val_dataset is not None:
        val_metrics = {}
        for batch_idx, batch in enumerate(data_module.val_dataloader()):
            _, _loss_d = model._step('val', batch, batch_idx, log=False)
            for key in _loss_d:
                try:
                    val_metrics[key] += _loss_d[key].item() * data_module.batch_size
                except KeyError:
                    val_metrics[key] = _loss_d[key] * data_module.batch_size

        # For the testing set, batches may not be all the same size due to drop_last=False,
        # so we need to account for that.
        for key in val_metrics:
            val_metrics[key] /= len(data_module.val_dataset)
        wandb_logger.log_metrics(val_metrics)

    # If a OOD test set is specified in the data module, we'll obtain the metrics for that as well
    if data_module.test_dataset is not None:
        ood_test_metrics = {}
        for batch_idx, batch in enumerate(data_module.test_dataloader()):
            _, _loss_d = model._step('test', batch, batch_idx, log=False)
            for key in _loss_d:
                try:
                    ood_test_metrics[key] += _loss_d[key].item() * data_module.batch_size
                except KeyError:
                    ood_test_metrics[key] = _loss_d[key] * data_module.batch_size

        # For the testing set, batches may not be all the same size due to drop_last=False,
        # so we need to account for that.
        for key in ood_test_metrics:
            ood_test_metrics[key] /= len(data_module.test_dataset)
        wandb_logger.log_metrics(ood_test_metrics)

    # If a IID test set is specified in the data module, we'll obtain the metrics for that as well
    if data_module.iid_test_dataset is not None:
        iid_test_metrics = {}
        for batch_idx, batch in enumerate(data_module.iid_test_dataloader()):
            _, _loss_d = model._step('iid_test', batch, batch_idx, log=False)
            for key in _loss_d:
                try:
                    iid_test_metrics[key] += _loss_d[key].item(
                    ) * data_module.batch_size
                except KeyError:
                    iid_test_metrics[
                        key] = _loss_d[key] * data_module.batch_size

        # For the testing set, batches may not be all the same size due to drop_last=False,
        # so we need to account for that.
        for key in iid_test_metrics:
            iid_test_metrics[key] /= len(data_module.iid_test_dataset)
        wandb_logger.log_metrics(iid_test_metrics)

    wandb.finish()

    return model


class NPMCTrainer():

    def __init__(
        self,
        data_module,
        model_cls,
        wandb_entity: str | None = None,
        wandb_project: str | None = None,
        wandb_save_dir: str | None = None,
        wandb_config: dict | None = None,
        gpu: bool = False,
        n_available_devices: int = 4,
        train_single_fn: Callable = train_uv_model,
        models_per_device: int = 1,
        num_epochs=2000,
        lr_scheduler=ReduceLROnPlateauWithWarmup,
        lr_scheduler_kwargs=None,
        train_fn_kwargs={},
    ):
        self.data_module = data_module
        self.model_cls = model_cls
        self.train_single_fn = train_single_fn
        self.num_epochs = num_epochs
        self.lr_scheduler = lr_scheduler
        self.lr_scheduler_kwargs = lr_scheduler_kwargs

        self.wandb_entity = wandb_entity
        if wandb_project is None:
            self.wandb_project = 'default_project'
        else:
            self.wandb_project = wandb_project

        if wandb_save_dir is None:
            self.wandb_save_dir = os.environ['HOME']
        else:
            self.wandb_save_dir = wandb_save_dir

        if wandb_config is None:
            self.wandb_config = {}
        else:
            self.wandb_config = wandb_config

        self.wandb_config.update({
            'entity': self.wandb_entity,
            'project': self.wandb_project,
            'save_dir': self.wandb_save_dir
        })

        self.gpu = gpu
        self.n_available_devices = n_available_devices
        self.models_per_device = models_per_device
        self.train_fn_kwargs = train_fn_kwargs

    def train_one_model(self,
                        model_config: dict,
                        wandb_name: str | None = None,
                        device_id=None):
        if isinstance(device_id, str):
            device_id = int(device_id.split(':')[-1])

        # get a free gpu from the list
        trainer_device_config = {}
        if self.gpu:
            trainer_device_config['accelerator'] = 'gpu'
            trainer_device_config['devices'] = [device_id]
        else:
            trainer_device_config['accelerator'] = 'auto'

        wandb_config = self.wandb_config.copy()
        wandb_config['name'] = wandb_name

        try:
            model = self.train_single_fn(
                model_cls=self.model_cls,
                config=model_config,
                data_module=self.data_module,
                lr_scheduler=self.lr_scheduler,
                lr_scheduler_kwargs=self.lr_scheduler_kwargs,
                num_epochs=self.num_epochs,
                wandb_config=wandb_config,
                trainer_device_config=trainer_device_config,
                **self.train_fn_kwargs)
        except Exception as e:
            # We'll ignore this exception and let this singular job fail
            print(e)
            wandb.finish()

        return model

    def train_many_models(self,
                          model_configs: list[dict],
                          wandb_name: list | str | None = None,
                          device_ids: list | None = None):
        if wandb_name is None:
            wandb_name = [None] * len(model_configs)
        elif isinstance(wandb_name, str):
            wandb_name = [wandb_name] * len(model_configs)

        if device_ids is None:
            device_ids = [None] * len(model_configs)

        training_runs = []
        for model_config, model_name, device_id in zip(model_configs, wandb_name, device_ids):
            _run_config = {
                'model_config': model_config,
                'wandb_name': model_name,
                'device_id': device_id
            }
            training_runs.append(_run_config)

        if self.gpu:
            from gpuparallel import GPUParallel, delayed
            GPUParallel(n_gpu=self.n_available_devices,
                        n_workers_per_gpu=self.models_per_device)(
                            delayed(self.train_one_model)(**run_config)
                            for run_config in training_runs)
        else:
            from joblib import Parallel, delayed

            Parallel(n_jobs=self.n_available_devices * self.models_per_device)(
                delayed(self.train_one_model)(**run_config)
                for run_config in training_runs)


def get_metrics(model, dataset):
    output_type_sorted = None
    if hasattr(dataset[0], 'metadata') and dataset[0].metadata is not None:
        output_type_sorted = {}

    output = []
    for data in dataset:
        if output_type_sorted is not None:
            data_label = data.metadata['tags'][0]
        y_hat = model(**data.to_dict(), batch=None).detach()
        # Calculate the metrics
        _output = [
            torch.nn.functional.cosine_similarity(y_hat, data.log_y,
                                                  dim=1).mean().item(),
            torch.nn.functional.mse_loss(y_hat, data.log_y).item(),
            torch.nn.functional.cosine_similarity(y_hat[:, 200:257],
                                                  data.log_y[:, 200:257],
                                                  dim=1).mean().item(),
            torch.nn.functional.mse_loss(y_hat[:, 200:257],
                                         data.log_y[:, 200:257]).item()
        ]
        output.append(_output)
        if output_type_sorted is not None:
            try:
                output_type_sorted[data_label].append(_output)
            except KeyError:
                output_type_sorted[data_label] = [_output]

    output = torch.tensor(output)
    if output_type_sorted is not None:
        for key in output_type_sorted:
            output_type_sorted[key] = torch.tensor(output_type_sorted[key])

    return output, output_type_sorted


def log_additional_data(model, data_module, wandb_logger):
    columns = ['Cos Sim', 'MSE', 'UV Cos Sim', 'UV MSE']

    # Run the data metrics on the train data
    output, output_type_sorted = get_metrics(model, data_module.npmc_train)
    overall_train_metrics = {
        'train_mean': output.mean(0).tolist(),
        'train_std': output.std(0).tolist(),
        'train_min': output.min(0).values.tolist(),
        'train_max': output.max(0).values.tolist(),
        'train_median': output.median(0).values.tolist(),
    }
    df = DataFrame(
        overall_train_metrics,
        index=['cosine similarity', 'mse', 'UV cosine similarity', 'UV mse'])
    df.reset_index(inplace=True)
    df = df.rename(columns={'index': 'metric'})
    wandb_logger.log_table('overall_train_metrics', dataframe=df)
    # wandb.log({'overall_train_metrics': wandb.Table(dataframe=df)})
    violin_fig = get_violin_plot(output, 'Train')
    wandb_train_violin_fig = fig_to_wandb_image(violin_fig)
    # Close the figure
    plt.close(violin_fig)

    # Run the data metrics on the train data
    output, output_type_sorted = get_metrics(model, data_module.npmc_val)
    overall_val_metrics = {
        'val_mean': output.mean(0).tolist(),
        'val_std': output.std(0).tolist(),
        'val_min': output.min(0).values.tolist(),
        'val_max': output.max(0).values.tolist(),
        'val_median': output.median(0).values.tolist(),
    }
    df = DataFrame(
        overall_val_metrics,
        index=['cosine similarity', 'mse', 'UV cosine similarity', 'UV mse'])
    df.reset_index(inplace=True)
    df = df.rename(columns={'index': 'metric'})
    wandb_logger.log_table('overall_val_metrics', dataframe=df)
    # wandb.log({'overall_val_metrics': wandb.Table(dataframe=df)})
    violin_fig = get_violin_plot(output, 'Validation')
    wandb_val_violin_fig = fig_to_wandb_image(violin_fig)

    # Close the figure
    plt.close(violin_fig)

    # Run the data metrics on the test data
    output, output_type_sorted = get_metrics(model, data_module.npmc_test)
    overall_test_metrics = {
        'test_mean': output.mean(0).tolist(),
        'test_std': output.std(0).tolist(),
        'test_min': output.min(0).values.tolist(),
        'test_max': output.max(0).values.tolist(),
        'test_median': output.median(0).values.tolist(),
    }
    test_metrics_by_class = {
        key: item.mean(0).tolist()
        for key, item in output_type_sorted.items()
    }
    df = DataFrame(
        overall_test_metrics,
        index=['cosine similarity', 'mse', 'UV cosine similarity', 'UV mse'])
    df.reset_index(inplace=True)
    df = df.rename(columns={'index': 'metric'})
    wandb_logger.log_table('overall_test_metrics', dataframe=df)
    # wandb.log({'overall_test_metrics': wandb.Table(dataframe=df)})
    df = DataFrame(
        test_metrics_by_class,
        index=['cosine similarity', 'mse', 'UV cosine similarity', 'UV mse']).T
    df.reset_index(inplace=True)
    df = df.rename(columns={'index': 'metric'})
    wandb_logger.log_table('test_metrics_by_class', dataframe=df)
    # wandb.log({'test_metrics_by_class': wandb.Table(dataframe=df)})

    # Get the figures for the test data
    violin_fig = get_violin_plot(output, 'Test')
    wandb_test_violin_fig = fig_to_wandb_image(violin_fig)
    test_fig = get_test_figure(output_type_sorted)
    wandb_test_fig = fig_to_wandb_image(test_fig)

    # Close the figures
    plt.close(violin_fig)
    plt.close(test_fig)

    # yapf: disable
    # table = wandb.Table(columns=['Train Violin Plot', 'Validation Violin Plot',
    #                              'Test Violin Plot', 'Test Split Figure'],
    #                     data=[[wandb_train_violin_fig, wandb_val_violin_fig,
    #                            wandb_test_violin_fig, wandb_test_fig]])
    wandb_logger.log_table('Metric Figures',
                           columns=['Train Violin Plot', 'Validation Violin Plot',
                                    'Test Violin Plot', 'Test Split Figure'],
                           data=[[wandb_train_violin_fig, wandb_val_violin_fig,
                                  wandb_test_violin_fig, wandb_test_fig]])
    # yapf: enable

    # wandb.log({'Metric Figures': table})
    # wandb.log({
    #     'Train Violin Plot': wandb_train_violin_fig,
    #     'Validation Violin Plot': wandb_val_violin_fig,
    #     'Test Violin Plot': wandb_test_violin_fig,
    #     'Test Split Figure': wandb_test_fig
    # })
    # wandb_logger.log_image('Train Violin Plot', wandb_train_violin_fig)
    # wandb_logger.log_image('Validation Violin Plot', wandb_val_violin_fig)
    # wandb_logger.log_image('Test Violin Plot', wandb_test_violin_fig)
    # wandb_logger.log_image('Test Split Figure', wandb_test_fig)


def get_logged_data(model, data):
    y_hat, loss = model.evaluate_step(data)

    spectra_x = data.spectra_x.squeeze()
    npmc_spectrum = data.y.squeeze()
    pred_spectrum = np.power(10, y_hat.detach().numpy()).squeeze()

    fig = plt.figure(dpi=150)
    plt.plot(spectra_x, npmc_spectrum, label='NPMC', alpha=1)
    plt.plot(spectra_x, pred_spectrum, label='NN', alpha=0.5)
    plt.xlabel('Wavelength (nm)', fontsize=18)
    plt.ylabel('Relative Intensity (a.u.)', fontsize=18)
    plt.xticks(fontsize=14)
    plt.yticks(fontsize=14)
    plt.legend()
    plt.tight_layout()

    fig.canvas.draw()
    full_fig_data = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
    full_fig_data = full_fig_data.reshape(fig.canvas.get_width_height()[::-1] +
                                          (3, ))

    plt.ylim(0, 1e4)
    plt.tight_layout()
    fig.canvas.draw()
    fig_data = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
    fig_data = fig_data.reshape(fig.canvas.get_width_height()[::-1] + (3, ))

    nanoparticle = plot_nanoparticle(data.constraints,
                                     data.dopant_specifications,
                                     as_np_array=True)

    npmc_qy = npmc_spectrum[:data.idx_zero].sum(
    ) / npmc_spectrum[data.idx_zero:].sum()
    pred_qy = pred_spectrum[:data.idx_zero].sum(
    ) / pred_spectrum[data.idx_zero:].sum()

    plt.close(fig)

    return [
        wandb.Image(nanoparticle),
        wandb.Image(full_fig_data),
        wandb.Image(fig_data), loss, npmc_qy, pred_qy
    ]


def get_np_template_from_feature(types, volumes, compositions,
                                 feature_processor):
    possible_elements = feature_processor.possible_elements

    types = types.reshape(-1, len(possible_elements))
    compositions = compositions.reshape(-1, len(possible_elements))
    dopant_specifications = []
    for i in range(types.shape[0]):
        for j in range(types.shape[1]):
            dopant_specifications.append(
                (i, compositions[i][j].item(), possible_elements[j], 'Y'))

    layer_volumes = volumes.reshape(-1, len(possible_elements))[:, 0]
    cum_volumes = torch.cumsum(layer_volumes, dim=0)
    radii = torch.pow(cum_volumes * 3 / (4 * np.pi), 1 / 3) * 100
    constraints = [SphericalConstraint(radius.item()) for radius in radii]
    return constraints, dopant_specifications


def fig_to_wandb_image(fig):
    fig.canvas.draw()
    data = np.frombuffer(fig.canvas.tostring_rgb(), dtype=np.uint8)
    data = data.reshape(fig.canvas.get_width_height()[::-1] + (3, ))
    return wandb.Image(data)


def get_violin_plot(output, title='Test'):
    fig = plt.figure(dpi=150)
    ax = fig.add_subplot()
    ax1 = ax.twinx()
    columns = ['Cos Sim', 'MSE', 'UV Cos Sim', 'UV MSE']

    vp = ax.violinplot(output[..., 0].numpy(), [0],
                       showmeans=True,
                       showmedians=True)
    for pc in vp['bodies']:
        pc.set_facecolor('tab:blue')
        # pc.set_edgecolor('black')
        pc.set_alpha(0.65)
    for line_label in ['cbars', 'cmins', 'cmaxes', 'cmeans', 'cmedians']:
        vp[line_label].set_color('k')
        vp[line_label].set_linewidth(0.75)
    vp['cbars'].set_linewidth(0.25)
    vp['cmedians'].set_linestyle('--')
    vp = ax1.violinplot(output[..., 1].log10().numpy(), [1],
                        showmeans=True,
                        showmedians=True)
    for pc in vp['bodies']:
        pc.set_facecolor('tab:red')
        # pc.set_edgecolor('black')
        pc.set_alpha(0.65)
    for line_label in ['cbars', 'cmins', 'cmaxes', 'cmeans', 'cmedians']:
        vp[line_label].set_color('k')
        vp[line_label].set_linewidth(0.75)
    vp['cbars'].set_linewidth(0.25)
    vp['cmedians'].set_linestyle('--')
    vp = ax.violinplot(output[..., 2].numpy(), [2],
                       showmeans=True,
                       showmedians=True)
    for pc in vp['bodies']:
        pc.set_facecolor('tab:blue')
        # pc.set_edgecolor('black')
        pc.set_alpha(0.65)
    for line_label in ['cbars', 'cmins', 'cmaxes', 'cmeans', 'cmedians']:
        vp[line_label].set_color('k')
        vp[line_label].set_linewidth(0.75)
    vp['cbars'].set_linewidth(0.25)
    vp['cmedians'].set_linestyle('--')
    vp = ax1.violinplot(output[..., 3].log10().numpy(), [3],
                        showmeans=True,
                        showmedians=True)
    for pc in vp['bodies']:
        pc.set_facecolor('tab:red')
        # pc.set_edgecolor('black')
        pc.set_alpha(0.65)
    vp['cbars'].set_linewidth(0.25)
    for line_label in ['cbars', 'cmins', 'cmaxes', 'cmeans', 'cmedians']:
        vp[line_label].set_color('k')
        vp[line_label].set_linewidth(0.75)
    vp['cbars'].set_linewidth(0.25)
    vp['cmedians'].set_linestyle('--')

    ax.tick_params(axis='y', colors='tab:blue')
    ax.set_yticklabels(ax.get_yticklabels(), fontsize=14)
    ax.set_xticks([0, 1, 2, 3])
    ax.set_xticklabels(columns, fontsize=14)
    ax.set_ylabel('Cosine Similarity', color='tab:blue', fontsize=18)
    ax1.tick_params(axis='y', colors='tab:red')
    ax1.set_yticks(np.arange(-2, 1, 1))
    ax1.yaxis.set_major_formatter(
        mticker.StrMethodFormatter("$10^{{{x:.0f}}}$"))
    # ax1.set_yticklabels(ax1.get_yticklabels(), fontsize=14)
    ax1.set_ylabel(r'$log_{10}(MSE)$', color='tab:red', fontsize=18)
    ax.set_title(f"{title} Data Metrics", fontsize=20)
    plt.tight_layout()
    return fig


def get_test_figure(output_type_sorted):
    fig = plt.figure(dpi=150)
    ax = fig.add_subplot()
    ax1 = ax.twinx()
    ax1.semilogy()
    x_labels = list(output_type_sorted.keys())
    columns = ['Cos Sim', 'MSE', 'UV Cos Sim', 'UV MSE']

    for i, label in zip([0, 1, 2, 3], columns):
        x = torch.arange(len(x_labels))
        y = torch.tensor([_l.mean(0)[i] for _l in output_type_sorted.values()])
        # if i < 2:
        #     fmt = 'o'
        # else:
        #     fmt = 'D'
        if i % 2 == 0:
            color = 'tab:blue'
            fmt = 'o' if i < 2 else 'D'
            ax.plot(x, y, fmt, color=color, alpha=0.6, markeredgecolor='k')
        else:
            color = 'tab:red'
            fmt = 'o' if i < 2 else 'D'
            ax1.plot(x, y, fmt, color=color, alpha=0.6, markeredgecolor='k')

    legend_elements = [
        Line2D([0], [0],
               marker='o',
               color='k',
               label='Full Spectrum',
               markerfacecolor='grey',
               linewidth=0,
               alpha=0.6),
        Line2D([0], [0],
               marker='D',
               color='k',
               label='UV Section',
               markerfacecolor='grey',
               linewidth=0,
               alpha=0.6)
    ]
    plt.legend(handles=legend_elements, loc='center right')
    ax.tick_params(axis='y', colors='tab:blue')
    ax.set_yticklabels(ax.get_yticklabels(), fontsize=14)
    ax.set_xticks([0, 1, 2, 3])
    ax.set_xticklabels(columns, fontsize=14)
    ax.set_ylabel('Cosine Similarity', color='tab:blue', fontsize=18)
    ax1.tick_params(axis='y', which='both', colors='tab:red')
    ax1.set_yticklabels(ax1.get_yticklabels(), fontsize=14)
    ax1.set_ylabel('MSE', color='tab:red', fontsize=18)
    ax.set_xticks(range(len(x_labels)))
    ax.set_xticklabels(x_labels, rotation=55)
    plt.tight_layout()
    return fig
