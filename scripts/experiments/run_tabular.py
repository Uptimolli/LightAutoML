"""Run tabular automl using ClearML logging."""

from utils import Timer
from utils import install_lightautoml


install_lightautoml()

import argparse
import os

import clearml
import numpy as np
import pandas as pd

from sklearn.metrics import log_loss
from sklearn.metrics import roc_auc_score

from lightautoml.automl.presets.tabular_presets import TabularAutoML
from lightautoml.tasks import Task

RANDOM_STATE = 1234


def fix_labels(values, renamed_labels):  # noqa D103
    target_mapping = {n: x for (x, n) in enumerate(renamed_labels)}
    return list(map(target_mapping.get, values))


def main(  # noqa D103
    dataset_name: str,
    cpu_limit: int,
    memory_limit: int,
    save_model: bool,
    train=None,
    test=None,
    task_type=None,
    project=None,
    task_name=None,
    dataset_id=None,
):

    if train is not None and test is not None and task_type is not None:
        cml_task = clearml.Task.init(
            project_name=project if project else "junk",
            task_name=task_name if task_name else "Unnamed Task",
            reuse_last_task_id=False,
        )
        if dataset_id:
            cml_task.set_parameter("Args/dataset", dataset_id)
    else:
        cml_task = clearml.Task.get_task(clearml.config.get_remote_task_id())
        dataset = clearml.Dataset.get(dataset_id=None, dataset_name=dataset_name)
        dataset_local_path = dataset.get_local_copy()

        with open(os.path.join(dataset_local_path, "task_type.txt"), "r") as f:
            task_type = f.readline()
        train = pd.read_csv(os.path.join(dataset_local_path, "train.csv"))
        test = pd.read_csv(os.path.join(dataset_local_path, "test.csv"))

    logger = cml_task.get_logger()
    try:

        if task_type == "multilabel":
            target_name = [x for x in test.columns if x.startswith("target")]
        elif "target" in test.columns:
            target_name = "target"
        else:
            target_name = test.columns[-1]

        if task_type in ["binary", "multiclass", "multilabel"]:
            assert (
                train[target_name].nunique() == test[target_name].nunique()
            ), "train and test has different unique values."

            is_train_unique_ok = train[target_name].nunique() > 1
            is_test_unique_ok = test[target_name].nunique() > 1

            if isinstance(is_train_unique_ok, bool):
                assert is_train_unique_ok, "Only one class present in train target."
            else:
                (is_train_unique_ok).all(), "Only one class present in train target."

            if isinstance(is_test_unique_ok, bool):
                assert is_test_unique_ok, "Only one class present in test target."
            else:
                (is_test_unique_ok).all(), "Only one class present in test target."

        assert train[target_name].isnull().values.any() is np.False_, "train has nans in target."
        assert test[target_name].isnull().values.any() is np.False_, "test has nans in target."

        task = Task(task_type)

        # =================================== automl config:
        automl = TabularAutoML(
            debug=True,
            task=task,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            # timeout=600,
            # general_params={
            #     "use_algos": [["lgb", "xgb", "cb"]]
            #     "use_algos": [["nn", "mlp", "dense", "denselight", "resnet", "snn", "node", "autoint", "fttransformer"]]
            # },
            # gbm_pipeline_params={"lgb_params": {"verbose_eval": 1}, "xgb_params": {"verbose_eval": 1}},
            # ['nn', 'mlp', 'dense', 'denselight', 'resnet', 'snn', 'node', 'autoint', 'fttransformer'] or custom torch model
            # nn_params={"n_epochs": 10, "bs": 512, "num_workers": 0, "path_to_save": None, "freeze_defaults": True},
            # nn_pipeline_params={"use_qnt": True, "use_te": False},
            reader_params={
                #     # 'n_jobs': N_THREADS,
                #     "cv": 5,
                "random_state": RANDOM_STATE,
            },
        )
        # ===================================

        cml_task.connect(automl)

        kwargs = {}
        if save_model:
            kwargs["path_to_save"] = "model"

        with Timer() as timer_training:
            oof_predictions = automl.fit_predict(train, roles={"target": target_name}, verbose=10, **kwargs)

        # add and upload local file artifact
        cml_task.upload_artifact(
            name="model.joblib",
            artifact_object=os.path.join(
                "model.joblib",
            ),
        )

        with Timer() as timer_predict:
            test_predictions = automl.predict(test)

        if task_type == "binary":
            metric_oof = roc_auc_score(train[target_name].values, oof_predictions.data[:, 0])
            metric_ho = roc_auc_score(test[target_name].values, test_predictions.data[:, 0])

        elif task_type == "multiclass":
            try:
                metric_oof = log_loss(train[target_name].values, oof_predictions.data)
                metric_ho = log_loss(test[target_name], test_predictions.data)
            except:
                # Some datasets can have dtype=float of target,
                # so we must map labels for correct log_loss calculating (if we didn't calсulate it in the try block)
                metric_oof = log_loss(
                    fix_labels(values=train[target_name].values, renamed_labels=automl.targets_order),
                    oof_predictions.data,
                )
                metric_ho = log_loss(
                    fix_labels(values=test[target_name], renamed_labels=automl.targets_order), test_predictions.data
                )

        elif task_type == "reg":
            metric_oof = task.metric_func(train[target_name].values, oof_predictions.data[:, 0])
            metric_ho = task.metric_func(test[target_name].values, test_predictions.data[:, 0])

        elif task_type == "multilabel":
            metric_oof = task.metric_func(train[target_name].values, oof_predictions.data)
            metric_ho = task.metric_func(test[target_name].values, test_predictions.data)
        else:
            raise ValueError("Bad task type.")

        print(f"Score for out-of-fold predictions: {metric_oof}")
        print(f"Score for hold-out: {metric_ho}")
        print(f"Train duration: {timer_training.duration}")
        print(f"Predict duration: {timer_predict.duration}")

        logger.report_single_value("Metric OOF", metric_oof)
        logger.report_single_value("Metric HO", metric_ho)

        logger.report_single_value("Train duration", timer_training.duration)
        logger.report_single_value("Predict duration", timer_predict.duration)
    except Exception as e:
        print(f"Error: {e}")
        raise e
    finally:
        logger.flush()
        cml_task.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="")
    parser.add_argument("--dataset", type=str, help="dataset name or id", default="sampled_app_train")
    parser.add_argument("--cpu_limit", type=int, help="", default=8)
    parser.add_argument("--memory_limit", type=int, help="", default=16)
    parser.add_argument("--save_model", action="store_true")
    args = parser.parse_args()

    main(
        dataset_name=args.dataset, cpu_limit=args.cpu_limit, memory_limit=args.memory_limit, save_model=args.save_model
    )
