"""
A lot of overrides on top of KubernetesJobWatcher
1. handling resource version management
2. handling of problems during Pending/Running stages
3. fixing dbnd sig term handlers

"""
import logging
import os
import signal
import time
import typing

from airflow.contrib.executors.kubernetes_executor import KubernetesJobWatcher
from airflow.utils.state import State

from dbnd._core.current import is_verbose
from dbnd._core.errors import DatabandRuntimeError
from dbnd._core.errors.base import DatabandSigTermError


if typing.TYPE_CHECKING:
    from dbnd_docker.kubernetes.kube_dbnd_client import DbndKubernetesClient

logger = logging.getLogger(__name__)


def watcher_sig_handler(signal, frame):
    import sys

    logger.info("Watcher received signal %s, PID: %s. exiting...", signal, os.getpid())
    sys.exit(0)


class DbndKubernetesJobWatcher(KubernetesJobWatcher):
    def __init__(self, kube_dbnd, **kwargs):
        super(DbndKubernetesJobWatcher, self).__init__(**kwargs)
        self.kube_dbnd = kube_dbnd  # type: DbndKubernetesClient

    def run(self):
        """Performs watching"""
        # we are in the different process than Scheduler
        # 1. Must reset filesystem cache to avoid using out-of-cluster credentials within Kubernetes
        from targets.fs import reset_fs_cache

        reset_fs_cache()
        # 2. Must reset signal handlers to avoid driver and watcher sharing signal handlers

        signal.signal(signal.SIGINT, watcher_sig_handler)
        signal.signal(signal.SIGTERM, watcher_sig_handler)
        signal.signal(signal.SIGQUIT, watcher_sig_handler)

        kube_client = self.kube_dbnd.kube_client
        try:
            while True:
                try:
                    self.resource_version = self._run(
                        kube_client,
                        self.resource_version,
                        self.worker_uuid,
                        self.kube_config,
                    )
                except DatabandSigTermError:
                    break
                except Exception:
                    self.log.exception("Unknown error in KubernetesJobWatcher. Failing")
                    raise
                else:
                    self.log.info(
                        "KubernetesWatcher restarting with resource_version: %s in %s seconds",
                        self.resource_version,
                        self.kube_dbnd.engine_config.watcher_recreation_interval_seconds,
                    )
                    time.sleep(
                        self.kube_dbnd.engine_config.watcher_recreation_interval_seconds
                    )
        except (KeyboardInterrupt, DatabandSigTermError):
            pass

    def _run(self, kube_client, resource_version, worker_uuid, kube_config):
        self.log.info(
            "Event: and now my watch begins starting at resource_version: %s. Watcher PID: %s",
            resource_version,
            os.getpid(),
        )

        from kubernetes import watch

        watcher = watch.Watch()
        request_timeout = self.kube_dbnd.engine_config.watcher_request_timeout_seconds
        kwargs = {
            "label_selector": "airflow-worker={}".format(worker_uuid),
            "_request_timeout": (request_timeout, request_timeout),
            "timeout_seconds": self.kube_dbnd.engine_config.watcher_client_timeout_seconds,
        }
        if resource_version:
            kwargs["resource_version"] = resource_version
        if kube_config.kube_client_request_args:
            for key, value in kube_config.kube_client_request_args.items():
                kwargs[key] = value

        for event in watcher.stream(
            kube_client.list_namespaced_pod, self.namespace, **kwargs
        ):
            try:
                # DBND PATCH
                # we want to process the message
                task = event["object"]
                self.log.debug(
                    " %s had an event of type %s", task.metadata.name, event["type"],
                )

                if event["type"] == "ERROR":
                    return self.process_error(event)

                self._extended_process_state(event)

                self.process_status(
                    task.metadata.name,
                    task.status.phase,
                    task.metadata.labels,
                    task.metadata.resource_version,
                )
                self.resource_version = task.metadata.resource_version

            except Exception as e:
                msg = "Event: Exception raised on specific event: %s, Exception: %s" % (
                    event,
                    e,
                )
                if is_verbose():
                    self.log.exception(msg)
                else:
                    self.log.warning(msg)
        return self.resource_version

    def _extended_process_state(self, event):
        pod_data = event["object"]
        pod_id = pod_data.metadata.name
        phase = pod_data.status.phase
        resource_version = pod_data.metadata.resource_version
        if phase == "Pending":
            # from Airflow 2.0 -> k8s may delete pods (preemption?)
            if event["type"] == "DELETED":
                self.log.info(
                    "Event: Failed to start pod %s: deleted, will reschedule", pod_id
                )
                self.watcher_queue.put(
                    (
                        pod_id,
                        State.UP_FOR_RESCHEDULE,
                        pod_data.metadata.labels,
                        resource_version,
                    )
                )
            else:
                pod_ctrl = self.kube_dbnd.get_pod_ctrl(
                    pod_id, namespace=pod_data.metadata.namespace
                )
                try:
                    # now we only fail, we will use the same code to try to rerun at scheduler code
                    pod_ctrl.check_deploy_errors(pod_data)
                except Exception as ex:
                    self.log.info(
                        "Event: Failed to start pod %s: %s, will reschedule",
                        pod_id,
                        str(ex),
                    )
                    self.watcher_queue.put(
                        (
                            pod_id,
                            State.FAILED,
                            pod_data.metadata.labels,
                            resource_version,
                        )
                    )
        elif phase == "Running":
            self.watcher_queue.put(
                (pod_id, State.RUNNING, pod_data.metadata.labels, resource_version)
            )

    def process_error(self, event):
        # DBND-AIRFLOW: copy of original, removed log line with error to prevent redundant log
        # there is no actual error, just reset of resource version)
        # self.log.error(
        #     'Encountered Error response from k8s list namespaced pod stream => %s',
        #     event
        # )
        raw_object = event["raw_object"]
        if raw_object["code"] == 410:
            self.log.info(
                "Kubernetes resource version is too old, resetting to 0 => %s",
                (raw_object["message"],),
            )
            # Return resource version 0
            return "0"
        raise DatabandRuntimeError(
            "Kubernetes failure for %s with code %s and message: %s"
            % (raw_object["reason"], raw_object["code"], raw_object["message"])
        )

    def safe_terminate(self):
        """
        This functions is a workaround for watcher is being alive,
        after it's terminated by Executor. If termination happens
        while watcher fetch data from k8s, `SIGTERM` might be handled by internal
        kubeclient implementation, and the watcher will stay running forever.
        Executor will be stacked at "kube_watcher.join()" call.

        Workaround: try to kill watcher multiple times.
        """
        if self.is_alive():
            logger.info("Terminating KubernetesJobWatcher process pid=%s", self.pid)
            for x in range(10):
                self.terminate()
                # first wait 10 seconds to stop
                self.join(timeout=10)
                if not self.is_alive():
                    logger.info("KubernetesJobWatcher has been succesfully terminated")
                    return
                logger.info(
                    "KubernetesJobWatcher is still running after being terminated"
                )
            logger.info(
                "Killing KubernetesJobWatcher on pid %s with -9 and wait for 3 seconds",
                self.pid,
            )
            os.kill(self.pid, signal.SIGKILL)
            time.sleep(3)