import logging

from pprint import pprint

import dateutil.parser

from dbnd import get_databand_context


logger = logging.getLogger(__name__)


class DatabandClient(object):
    """
    Customer facing api client
    """

    def __init__(self, api_client, verbose=False):
        self.verbose = verbose
        self.api_client = api_client

    def get_task_run_info(self, task_af_id):
        task_runs_info = self.api_client.api_request(
            "/api/client/v1/task_run_info",
            None,
            method="GET",
            query={
                "filter": '[{"name":"task_af_id","op":"eq","val": "%s"}]' % task_af_id
            },
        )
        task_runs_info = task_runs_info["data"]
        if not task_runs_info:
            raise Exception("Task run not found: %s", task_af_id)
        if len(task_runs_info) > 1:
            logger.warning(
                "More than one task found for %s, returning the last one" % task_af_id
            )

        # return the last one
        # TODO: we need much better query
        return task_runs_info[-1]

    def get_metrics(self, task_run_attempt_uid, source=None):
        query = {
            "task_run_attempt_uid": task_run_attempt_uid,
        }
        if source:
            query["source"] = source
        metrics = self.api_client.api_request(
            "/api/client/v1/metrics", None, method="GET", query=query
        )

        if self.verbose:
            logger.info(
                "Metrics for %s: %s", task_run_attempt_uid, pprint(metrics["data"])
            )
        return metrics["data"]

    def get_metrics_as_dict(
        self, task_run_attempt_uid, source=None, filter_name_by_prefix=None
    ):
        metrics = self.get_metrics(
            task_run_attempt_uid=task_run_attempt_uid, source=source
        )
        if filter_name_by_prefix:
            # Generator!
            # TODO: move to webserver
            metrics = (m for m in metrics if m["key"].startswith(filter_name_by_prefix))
        return {m["key"]: m for m in metrics}

    def get_run_info(self, run_uid):
        run_info = self.api_client.api_request(
            "/api/client/v1/run/%s" % run_uid, None, method="GET",
        )
        return run_info

    def _get_task_start_time(self, task_run):
        return dateutil.parser.isoparse(
            task_run["latest_task_run_attempt"].get("end_date", None)
        )

    def get_first_task_run_error(self, run_uid):
        run_info = self.get_run_info(run_uid)
        first_error_time = first_error_task = None
        for task_run in run_info["task_runs"]:
            task_time = self._get_task_start_time(task_run)
            if task_time is None:
                continue
            if first_error_time is None or task_time < first_error_time:
                if task_run["latest_error"] is not None:
                    first_error_time, first_error_task = task_time, task_run

        if first_error_task is None:
            return None

        return first_error_task["latest_error"]

    @classmethod
    def build_databand_client(cls):
        api_client = get_databand_context().databand_api_client
        return DatabandClient(api_client, verbose=True)
