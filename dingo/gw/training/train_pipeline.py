import os

import numpy as np
import yaml
import argparse
import textwrap
import torch
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group

from threadpoolctl import threadpool_limits

from dingo.core.posterior_models.build_model import autocomplete_model_kwargs, build_model_from_kwargs
from dingo.gw.training.train_builders import (
    build_dataset,
    set_train_transforms,
    build_svd_for_embedding_network,
)
from dingo.core.utils.trainutils import RuntimeLimits
from dingo.core.utils import (
    set_requires_grad_flag,
    get_number_of_model_parameters,
    build_train_and_test_loaders,
)
from dingo.core.utils.trainutils import EarlyStopping


def prepare_training_new(train_settings: dict, train_dir: str, local_settings: dict):
    """
    Based on a settings dictionary, initialize a WaveformDataset and PosteriorModel.

    For model type 'nsf+embedding' (the only acceptable type at this point) this also
    initializes the embedding network projection stage with SVD V matrices based on
    clean detector waveforms.

    Parameters
    ----------
    train_settings : dict
        Settings which ultimately come from train_settings.yaml file.
    train_dir : str
        This is only used to save diagnostics from the SVD.
    local_settings : dict
        Local settings (e.g., num_workers, device)

    Returns
    -------
    (WaveformDataset, BasePosteriorModel)
    """

    wfd = build_dataset(train_settings["data"])  # No transforms yet
    initial_weights = {}

    # The embedding network is assumed to have an SVD projection layer. If other types
    # of embedding networks are added in the future, update this code.

    if train_settings["model"].get("embedding_kwargs", None):
        # First, build the SVD for seeding the embedding network.
        print("\nBuilding SVD for initialization of embedding network.")
        initial_weights["V_rb_list"] = build_svd_for_embedding_network(
            wfd,
            train_settings["data"],
            train_settings["training"]["stage_0"]["asd_dataset_path"],
            num_workers=local_settings["num_workers"],
            batch_size=train_settings["training"]["stage_0"]["batch_size"],
            out_dir=train_dir,
            **train_settings["model"]["embedding_kwargs"]["svd"],
        )

    # Now set the transforms for training. We need to do this here so that we can (a)
    # get the data dimensions to configure the network, and (b) save the
    # parameter standardization dict in the PosteriorModel. In principle, (a) could
    # be done without generating data (by careful calculation) and (b) could also
    # be done outside the transform setup. But for now, this is convenient. The
    # transforms will be reset later by initialize_stage().

    set_train_transforms(
        wfd,
        train_settings["data"],
        train_settings["training"]["stage_0"]["asd_dataset_path"],
    )

    # This modifies the model settings in-place.
    autocomplete_model_kwargs(train_settings["model"], wfd[0])
    full_settings = {
        "dataset_settings": wfd.settings,
        "train_settings": train_settings,
    }

    print("\nInitializing new posterior model.")
    print("Complete settings:")
    print(yaml.dump(full_settings, default_flow_style=False, sort_keys=False))

    pm = build_model_from_kwargs(
        settings=full_settings,
        initial_weights=initial_weights,
        device=local_settings["device"],
    )

    if local_settings.get("wandb", False):
        try:
            import wandb

            wandb.init(
                config=full_settings,
                dir=train_dir,
                **local_settings["wandb"],
            )
        except ImportError:
            print("WandB is enabled but not installed.")

    return pm, wfd


def prepare_training_resume(checkpoint_name, local_settings, train_dir):
    """
    Loads a PosteriorModel from a checkpoint, as well as the corresponding
    WaveformDataset, in order to continue training. It initializes the saved optimizer
    and scheduler from the checkpoint.

    Parameters
    ----------
    checkpoint_name : str
        File name containing the checkpoint (.pt format).
    device : str
        'cuda' or 'cpu'

    Returns
    -------
    (BasePosteriorModel, WaveformDataset)
    """

    pm = build_model_from_kwargs(
        filename=checkpoint_name, device=local_settings["device"]
    )
    wfd = build_dataset(pm.metadata["train_settings"]["data"])

    if local_settings.get("wandb", False):
        try:
            import wandb

            wandb.init(
                resume="must",
                dir=train_dir,
                **local_settings["wandb"],
            )
        except ImportError:
            print("WandB is enabled but not installed.")

    return pm, wfd


def initialize_stage(pm, wfd, stage, num_workers, resume=False, world_size = 1):
    """
    Initializes training based on PosteriorModel metadata and current stage:
        * Builds transforms (based on noise settings for current stage);
        * Builds DataLoaders;
        * At the beginning of a stage (i.e., if not resuming mid-stage), initializes
        a new optimizer and scheduler;
        * Freezes / unfreezes SVD layer of embedding network

    Parameters
    ----------
    pm : BasePosteriorModel
    wfd : WaveformDataset
    stage : dict
        Settings specific to current stage of training
    num_workers : int
    resume : bool
        Whether training is resuming mid-stage. This controls whether the optimizer and
        scheduler should be re-initialized based on contents of stage dict.

    Returns
    -------
    (train_loader, test_loader)
    """

    train_settings = pm.metadata["train_settings"]

    # Rebuild transforms based on possibly different noise.
    set_train_transforms(wfd, train_settings["data"], stage["asd_dataset_path"])

    # Allows for changes in batch size between stages.
    train_loader, test_loader = build_train_and_test_loaders(
        wfd,
        train_settings["data"]["train_fraction"],
        stage["batch_size"] // world_size,
        num_workers // world_size,
    )

    if not resume:
        # New optimizer and scheduler. If we are resuming, these should have been
        # loaded from the checkpoint.
        print("Initializing new optimizer and scheduler.")
        pm.optimizer_kwargs = stage["optimizer"]
        pm.scheduler_kwargs = stage["scheduler"]
        pm.initialize_optimizer_and_scheduler()

    # Freeze/unfreeze RB layer if necessary
    if "freeze_rb_layer" in stage:
        if stage["freeze_rb_layer"]:
            set_requires_grad_flag(
                pm.network, name_contains="layers_rb", requires_grad=False
            )
        else:
            set_requires_grad_flag(
                pm.network, name_contains="layers_rb", requires_grad=True
            )
    n_grad = get_number_of_model_parameters(pm.network, (True,))
    n_nograd = get_number_of_model_parameters(pm.network, (False,))
    print(f"Fixed parameters: {n_nograd}\nLearnable parameters: {n_grad}\n")

    return train_loader, test_loader


def train_stages(pm, wfd, train_dir, local_settings, rank = 0, world_size = 1):
    """
    Train the network, iterating through the sequence of stages. Stages can change
    certain settings such as the noise characteristics, optimizer, and scheduler settings.

    Parameters
    ----------
    pm : BasePosteriorModel
    wfd : WaveformDataset
    train_dir : str
        Directory for saving checkpoints and train history.
    local_settings : dict

    Returns
    -------
    bool
        True if all stages are complete
        False otherwise
    """

    train_settings = pm.metadata["train_settings"]
    runtime_limits = RuntimeLimits(
        epoch_start=pm.epoch, **local_settings["runtime_limits"]
    )

    # Extract list of stages from settings dict
    stages = []
    num_stages = 0
    while True:
        try:
            stages.append(train_settings["training"][f"stage_{num_stages}"])
            num_stages += 1
        except KeyError:
            break
    end_epochs = list(np.cumsum([stage["epochs"] for stage in stages]))

    num_starting_stage = np.searchsorted(end_epochs, pm.epoch + 1)
    for n in range(num_starting_stage, num_stages):
        stage = stages[n]

        if pm.epoch == end_epochs[n] - stage["epochs"]:
            print(f"\nBeginning training stage {n}. Settings:")
            print(yaml.dump(stage, default_flow_style=False, sort_keys=False))
            train_loader, test_loader = initialize_stage(
                pm, wfd, stage, local_settings["num_workers"], resume=False, world_size=world_size
            )
        else:
            print(f"\nResuming training in stage {n}. Settings:")
            print(yaml.dump(stage, default_flow_style=False, sort_keys=False))
            train_loader, test_loader = initialize_stage(
                pm, wfd, stage, local_settings["num_workers"], resume=True, world_size=world_size
            )
        early_stopping = None
        if stage.get("early_stopping"):
            try: 
                early_stopping = EarlyStopping(**stage["early_stopping"])
            except Exception:
                print("Early stopping settings invalid. Please pass 'patience', 'delta', 'metric'")
                raise
            

        runtime_limits.max_epochs_total = end_epochs[n]
        pm.train(
            rank,
            train_loader,
            test_loader,
            train_dir=train_dir,
            runtime_limits=runtime_limits,
            checkpoint_epochs=local_settings["checkpoint_epochs"],
            use_wandb=local_settings.get("wandb", False),
            test_only=local_settings.get("test_only", False),
            early_stopping=early_stopping,
        )
        # if test_only, model should not be saved, and run is complete
        if local_settings.get("test_only", False):
            return True

        if pm.epoch == end_epochs[n]:
            save_file = os.path.join(train_dir, f"model_stage_{n}.pt")
            print(f"Training stage complete. Saving to {save_file}.")
            pm.save_model(save_file, save_training_info=True)
        if runtime_limits.local_limits_exceeded(pm.epoch):
            print("Local runtime limits reached. Ending program.")
            break

    if pm.epoch == end_epochs[-1]:
        return True
    else:
        return False


def parse_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
        Train a neural network for gravitational-wave single-event inference.
        
        This program can be called in one of two ways:
            a) with a settings file. This will create a new network based on the 
            contents of the settings file.
            b) with a checkpoint file. This will resume training from the checkpoint.
        """
        ),
    )
    parser.add_argument(
        "--settings_file",
        type=str,
        help="YAML file containing training settings.",
    )
    parser.add_argument(
        "--train_dir", required=True, help="Directory for Dingo training output."
    )
    parser.add_argument(
        "--checkpoint",
        type=str,
        help="Checkpoint file from which to resume training.",
    )
    parser.add_argument(
        "--exit_command",
        type=str,
        default="",
        help="Optional command to execute after completion of training.",
    )
    args = parser.parse_args()

    # The settings file and checkpoint are mutually exclusive.
    if args.checkpoint is None and args.settings_file is None:
        parser.error("Must specify either a checkpoint file or a settings file.")
    if args.checkpoint is not None and args.settings_file is not None:
        parser.error("Cannot specify both a checkpoint file and a settings file.")

    return args

def resume_run_main(rank, world_size, checkpoint, local_settings, train_dir):
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "18848"
    torch.cuda.set_device(rank)
    init_process_group(backend="nccl", rank=rank, world_size=world_size)
    local_settings["device"] = rank

    print(f'Init rank {rank}')
    
    pm, wfd = prepare_training_resume(
        checkpoint, local_settings, train_dir
    )

    pm.network = DDP(pm.network, device_ids=[rank], find_unused_parameters=True)

    with threadpool_limits(limits=1, user_api="blas"):
        train_stages(pm, wfd, train_dir, local_settings, rank=rank, world_size=world_size)
    
    destroy_process_group()


def train_local():
    args = parse_args()

    os.makedirs(args.train_dir, exist_ok=True)

    if args.settings_file is not None:
        print("Beginning new training run.")
        with open(args.settings_file, "r") as fp:
            train_settings = yaml.safe_load(fp)

        # Extract the local settings from train settings file, save it separately. This
        # file can later be modified, and the settings take effect immediately upon
        # resuming.

        local_settings = train_settings.pop("local")
        with open(os.path.join(args.train_dir, "local_settings.yaml"), "w") as f:
            if (
                local_settings.get("wandb", False)
                and "id" not in local_settings["wandb"].keys()
            ):
                try:
                    import wandb

                    local_settings["wandb"]["id"] = wandb.util.generate_id()
                except ImportError:
                    print("wandb not installed, cannot generate run id.")
            yaml.dump(local_settings, f, default_flow_style=False, sort_keys=False)

        pm, wfd = prepare_training_new(train_settings, args.train_dir, local_settings)

        with threadpool_limits(limits=1, user_api="blas"):
            complete = train_stages(pm, wfd, args.train_dir, local_settings)

        if complete:
            if args.exit_command:
                print(
                    f"All training stages complete. Executing exit command: {args.exit_command}."
                )
                os.system(args.exit_command)
            else:
                print("All training stages complete.")
        else:
            print("Program terminated due to runtime limit.")

    else:
        print("Resuming training run.")
        with open(os.path.join(args.train_dir, "local_settings.yaml"), "r") as f:
            local_settings = yaml.safe_load(f)
        
        world_size = torch.cuda.device_count()
        mp.spawn(resume_run_main, args=(world_size, args.checkpoint, local_settings, args.train_dir), nprocs=world_size)
