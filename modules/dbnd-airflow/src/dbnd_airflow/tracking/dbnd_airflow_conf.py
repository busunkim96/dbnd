from dbnd._core.configuration.environ_config import (
    DBND_PARENT_TASK_RUN_ATTEMPT_UID,
    DBND_PARENT_TASK_RUN_UID,
)
from dbnd._core.settings import CoreConfig, TrackingConfig


def get_airflow_conf(
    dag_id="{{dag.dag_id}}",
    task_id="{{task.task_id}}",
    execution_date="{{ts}}",
    try_number="{{task_instance._try_number}}",
):
    """
    These properties are
        AIRFLOW_CTX_DAG_ID - name of the Airflow DAG to associate a run with
        AIRFLOW_CTX_EXECUTION_DATE - execution_date to associate a run with
        AIRFLOW_CTX_TASK_ID - name of the Airflow Task to associate a run with
        AIRFLOW_CTX_TRY_NUMBER - try number of the Airflow Task to associate a run with
    """
    airflow_conf = {
        "AIRFLOW_CTX_DAG_ID": dag_id,
        "AIRFLOW_CTX_EXECUTION_DATE": execution_date,
        "AIRFLOW_CTX_TASK_ID": task_id,
        "AIRFLOW_CTX_TRY_NUMBER": try_number,
    }
    airflow_conf.update(get_databand_url_conf())
    return airflow_conf


def _get_databand_url():
    try:
        external = TrackingConfig().databand_external_url
        if external:
            return external
        return CoreConfig().databand_url
    except Exception:
        pass


def get_databand_url_conf():
    databand_url = _get_databand_url()
    if databand_url:
        return {"DBND__CORE__DATABAND_URL": databand_url}
    return {}


def extract_airflow_tracking_conf(context):
    conf = extract_airflow_conf(context)
    conf.update(get_databand_url_conf())
    return conf


def extract_airflow_conf(context):
    task_instance = context.get("task_instance")
    if task_instance is None:
        return {}

    dag_id = task_instance.dag_id
    task_id = task_instance.task_id
    execution_date = str(task_instance.execution_date)
    try_number = str(task_instance.try_number)

    if dag_id and task_id and execution_date:
        return {
            "AIRFLOW_CTX_DAG_ID": dag_id,
            "AIRFLOW_CTX_EXECUTION_DATE": execution_date,
            "AIRFLOW_CTX_TASK_ID": task_id,
            "AIRFLOW_CTX_TRY_NUMBER": try_number,
        }
    return {}


def get_env_dbnd_track(context, task_run):
    envs = extract_airflow_conf(context)
    return extend_airflow_ctx_env_with_dbnd(task_run, envs)


def extend_airflow_ctx_env_with_dbnd(task_run, airflow_ctx_env):
    envs = airflow_ctx_env.copy()
    envs[DBND_PARENT_TASK_RUN_UID] = str(task_run.task_run_uid)
    envs[DBND_PARENT_TASK_RUN_ATTEMPT_UID] = str(task_run.task_run_attempt_uid)
    return envs
