from multiprocessing.sharedctypes import RawValue
import tempfile
import subprocess
from tabsynth import  lib
import os
import optuna
import argparse
from pathlib import Path
# from azureml.core import Run
from tabsynth.CTGAN.train_sample_tvae import train_tvae, sample_tvae
from tabsynth.scripts.eval_catboost import train_catboost
from tabsynth.scripts.eval_similarity import calculate_similarity_score
import sys

parser = argparse.ArgumentParser()
parser.add_argument('data_path', type=str)
parser.add_argument('train_size', type=int)
parser.add_argument('eval_type', type=str)
parser.add_argument('device', type=str)
parser.add_argument("--optimize_sim_score", action='store_true', default=False)
parser.add_argument("--debug", action='store_true', default=False)

args = parser.parse_args()
# run = Run.get_context()
real_data_path = args.data_path
eval_type = args.eval_type
train_size = args.train_size
device = args.device
assert eval_type in ('merged', 'synthetic')
config_path = real_data_path.replace('data', 'exp')
raw_config = lib.load_config(os.path.join(config_path, "config.toml"))
# raw_config =  {}
def objective(trial):
    
    lr = trial.suggest_loguniform('lr', 0.00001, 0.003)

    def suggest_dim(name):
        t = trial.suggest_int(name, d_min, d_max)
        return 2 ** t
    
    # construct model
    min_n_layers, max_n_layers, d_min, d_max = 1, 3, 6, 9
    n_layers = 2 * trial.suggest_int('n_layers', min_n_layers, max_n_layers)
    d_first = [suggest_dim('d_first')] if n_layers else []
    d_middle = (
        [suggest_dim('d_middle')] * (n_layers - 2)
        if n_layers > 2
        else []
    )
    d_last = [suggest_dim('d_last')] if n_layers > 1 else []
    d_layers = d_first + d_middle + d_last
    ####

    steps = trial.suggest_categorical('steps', [5000, 20000, 30000])
    # steps = trial.suggest_categorical('steps', [1000])
    batch_size = trial.suggest_categorical('batch_size', [256, 4096])

    num_samples = int(train_size * (2 ** trial.suggest_int('frac_samples', -2, 3)))
    embedding_dim = 2 ** trial.suggest_int('embedding_dim', 6, 10)
    loss_factor = trial.suggest_loguniform('loss_factor', 0.001, 10)



    train_params = {
        "lr": lr,
        "epochs": steps,
        "embedding_dim": embedding_dim,
        "batch_size": batch_size,
        "loss_factor": loss_factor,
        "compress_dims": d_layers,
        "decompress_dims": d_layers
    }

    if args.debug:
        train_params["epochs"] = 10
        num_samples = 1000
        train_params["batch_size"] = 32
        steps = 1000

    trial.set_user_attr("train_params", train_params)
    trial.set_user_attr("num_samples", num_samples)



    score = 0.0
    with tempfile.TemporaryDirectory() as dir_:
        dir_ = Path(dir_)
        ctabgan = train_tvae(
            parent_dir=dir_,
            real_data_path=real_data_path,
            train_params=train_params,
            change_val=True,
            device=device
        )
        sim_score = []
        for sample_seed in range(5):
            sample_tvae(
                ctabgan,
                parent_dir=dir_,
                real_data_path=real_data_path,
                num_samples=num_samples,
                train_params=train_params,
                change_val=True,
                seed=sample_seed,
                device=device
            )

            T_dict = {
                "seed": 0,
                "normalization": None,
                "num_nan_policy": None,
                "cat_nan_policy": None,
                "cat_min_frequency": None,
                "cat_encoding": None,
                "y_policy": "default"
            }
            metrics = train_catboost(
                parent_dir=dir_,
                real_data_path=real_data_path, 
                eval_type=eval_type,
                T_dict=T_dict,
                change_val=True,
                seed = 0
            )
            sim_report = calculate_similarity_score(
                parent_dir=dir_,
                real_data_path=real_data_path,
                eval_type=eval_type,
                num_classes=raw_config['model_params']['num_classes'],
                # is_y_cond=False,
                T_dict=raw_config['eval']['T'],
                seed=0,
                change_val=True,
                table_evaluate=False,
            )
            sim_score.append(sim_report['sim_score'])
            score += metrics.get_val_score()
    # calculate the average score
    print(f"Average similarity results:")
    for k, v in lib.average_per_key(sim_score).items():
        # run.log(k, v)
        print(f"{k}: {v}")
    if args.optimize_sim_score:
        print("optimizing for similarity score")
        return lib.average_per_key(sim_score)['score-mean']
    else:
        print(f"optimizing for {args.eval_model} score")
        return score / 5


study = optuna.create_study(
    direction='maximize',
    sampler=optuna.samplers.TPESampler(seed=0),
)
n_trials = 50 if not args.debug else 10

study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

os.makedirs(f"exp/{Path(real_data_path).name}/tvae/", exist_ok=True)
config = {
    "parent_dir": f"exp/{Path(real_data_path).name}/tvae/",
    "real_data_path": real_data_path,
    "seed": 0,
    "device": args.device,
    "train_params": study.best_trial.user_attrs["train_params"],
    "sample": {"seed": 0, "num_samples": study.best_trial.user_attrs["num_samples"]},
    "eval": {
        "type": {"eval_model": "catboost", "eval_type": eval_type},
        "T": {
            "seed": 0,
            "normalization": None,
            "num_nan_policy": None,
            "cat_nan_policy": None,
            "cat_min_frequency": None,
            "cat_encoding": None,
            "y_policy": "default"
        },
    }
}

train_tvae(
    parent_dir=f"exp/{Path(real_data_path).name}/tvae/",
    real_data_path=real_data_path,
    train_params=study.best_trial.user_attrs["train_params"],
    change_val=False,
    device=device
)

lib.dump_config(config, config["parent_dir"]+"config.toml")

my_env = os.environ.copy()
my_env["PYTHONPATH"] = os.getcwd() # Needed to run the subscripts
subprocess.run([sys.executable, "scripts/eval_seeds.py", '--config', f'{config["parent_dir"]+"config.toml"}',
                '10', "tvae", eval_type, "catboost", "5"], check=True, env=my_env)