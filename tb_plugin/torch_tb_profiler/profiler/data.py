# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# --------------------------------------------------------------------------
import gzip
import io as sysio
import json
import re
import tempfile
from json.decoder import JSONDecodeError

from .. import io, utils
from . import trace
from .communication import analyze_communication_nodes
from .event_parser import EventParser, ProfileRole
from .gpu_metrics_parser import GPUMetricsParser
from .kernel_parser import KernelParser
from .memory_parser import MemoryParser
from .module_parser import ModuleAggregator
from .overall_parser import OverallParser

logger = utils.get_logger()

class RunProfileData(object):
    def __init__(self, worker, span=None):
        self.worker = worker
        self.span = span
        self.data_schema_version = None
        self.distributed_info = None
        self.device_props = None
        self.used_devices = []
        self.use_dp = False
        self.use_ddp =False
        self.use_nccl = False
        self.events = None
        self.trace_file_path = None
        self.has_runtime = False
        self.has_kernel = False
        self.has_communication = False
        self.has_memcpy_or_memset = False
        self.steps_costs = None
        self.steps_names = None
        self.avg_costs = None
        self.runtime_node_list = None
        self.gpu_ids = None
        self.gpu_utilization = None
        self.sm_efficiency = None
        self.occupancy = None
        self.gpu_util_buckets = None  # Cached here. Will be processed to json on first trace view.
        self.approximated_sm_efficiency_ranges = None  # Cached here. Will be processed to json on first trace view.
        self.blocks_per_sm_count = None
        self.occupancy_count = None
        self.op_list_groupby_name = None
        self.op_list_groupby_name_input = None
        self.stack_lists_group_by_name = None
        self.stack_lists_group_by_name_input = None
        self.kernel_list_groupby_name_op = None
        self.kernel_stat = None
        self.tc_used_ratio = None
        self.recommendations = []
        self.comm_node_list = None
        self.comm_overlap_costs = None

        self.memory_stats = None
        self.memory_trace = None

    @property
    def has_memory_data(self):
        if self.memory_stats:
            for node_metrics in self.memory_stats.values():
                for metrics_values in node_metrics.values():
                    if any(metrics_values):
                        return True

        return False

    @staticmethod
    def parse(worker, span, path):
        trace_path, trace_json = RunProfileData._preprocess_file(path)

        profile = RunProfileData.from_json(worker, span, trace_json)
        profile.trace_file_path = trace_path
        return profile, trace_path

    @staticmethod
    def from_json(worker, span, trace_json):
        profile = RunProfileData(worker, span)
        profile.data_schema_version = trace_json.get("schemaVersion", None)
        profile.distributed_info = trace_json.get("distributedInfo", None)
        profile.device_props = trace_json.get("deviceProperties", None)
        trace_json = trace_json["traceEvents"]

        profile.events = []
        for data in trace_json:
            event = trace.create_event(data)
            if event is not None:
                profile.events.append(event)

        profile.process()
        profile.analyze()
        return profile

    @staticmethod
    def _preprocess_file(trace_path):
        if not io.exists(trace_path):
            raise FileNotFoundError(trace_path)

        data = io.read(trace_path)
        if trace_path.endswith('.gz'):
            data = gzip.decompress(data)

        json_reencode = False
        try:
            trace_json = json.loads(data)
        except JSONDecodeError as e:
            # Kineto may export json file with non-ascii code. before this is fixed, use a workaround
            # to handle JSONDecodeError, re-encode it and save to a temp file
            try:
                trace_json = json.loads(data, strict=False)
            except JSONDecodeError:
                with sysio.StringIO() as fout:
                    str_data = data.decode("utf-8")
                    # only replace the N/A without surrounding double quote
                    fout.write(re.sub(r'(?<!")N/A(?!")', "\"N/A\"", str_data))
                    trace_json = json.loads(fout.getvalue())
                    logger.warning("Get JSONDecodeError: %s, Re-encode it to temp file" % e.msg)
                    json_reencode = True

        # work-around to remove the "Record Window End" events to avoid the huge end timestamp
        event_list = trace_json["traceEvents"]
        end_index = None
        start_index = None
        for i in reversed(range(len(event_list))):
            if event_list[i]["name"] == "Record Window End":
                end_index = i
            elif event_list[i]["name"].startswith("Iteration Start:"):
                start_index = i
            if start_index is not None and end_index is not None:
                break

        if start_index is not None and end_index is not None:
            dur = event_list[end_index]["ts"] - event_list[start_index]["ts"]
            if dur > 24 * 3600 * 1000:
                del trace_json["traceEvents"][end_index]
                json_reencode = True

        if json_reencode:
            fp = tempfile.NamedTemporaryFile('w+t', suffix='.json.gz', delete=False)
            fp.close()
            with gzip.open(fp.name, mode='wt') as fzip:
                fzip.write(json.dumps(trace_json))
            trace_path = fp.name

        return trace_path, trace_json

    def process(self):
        parser = EventParser()
        tid2tree = parser.parse(self.events)

        self.has_runtime = parser.has_runtime
        self.has_kernel = parser.has_kernel
        self.has_communication = parser.has_communication
        self.has_memcpy_or_memset = parser.has_memcpy_or_memset
        self.steps_names = parser.steps_names
        self.used_devices = sorted(list(parser.used_devices))
        self.use_dp = parser.use_dp
        self.use_ddp = parser.use_ddp
        self.use_nccl = parser.use_nccl

        # Parse communications.
        self.comm_node_list = parser.generate_communication_nodes()

        # Starting aggregate
        logger.debug("ModuleAggregator")
        module_aggregator = ModuleAggregator()
        module_aggregator.aggregate(tid2tree)
        self.op_list_groupby_name = module_aggregator.op_list_groupby_name
        self.op_list_groupby_name_input = module_aggregator.op_list_groupby_name_input
        self.stack_lists_group_by_name = module_aggregator.stack_lists_group_by_name
        self.stack_lists_group_by_name_input = module_aggregator.stack_lists_group_by_name_input
        self.kernel_list_groupby_name_op = module_aggregator.kernel_list_groupby_name_op

        logger.debug("OverallParser")
        overall_parser = OverallParser()
        overall_parser.aggregate(parser.steps, parser.role_ranges)
        self.avg_costs = overall_parser.avg_costs
        self.steps_costs = overall_parser.steps_costs
        self.comm_overlap_costs = overall_parser.communication_overlap

        logger.debug("GPUMetricsParser")
        self.runtime_node_list = parser.runtime_node_list
        gpu_metrics_parser = GPUMetricsParser()
        gpu_metrics_parser.parse_events(self.events, parser.global_start_ts, parser.global_end_ts,
                                        parser.steps[0][0], parser.steps[-1][1])
        self.gpu_ids = gpu_metrics_parser.gpu_ids
        self.gpu_utilization = gpu_metrics_parser.gpu_utilization
        self.sm_efficiency = gpu_metrics_parser.avg_approximated_sm_efficiency_per_device
        self.occupancy = gpu_metrics_parser.avg_occupancy_per_device
        self.gpu_util_buckets = gpu_metrics_parser.gpu_util_buckets
        self.approximated_sm_efficiency_ranges = gpu_metrics_parser.approximated_sm_efficiency_ranges
        self.blocks_per_sm_count = gpu_metrics_parser.blocks_per_sm_count
        self.occupancy_count = gpu_metrics_parser.occupancy_count

        memory_parser = MemoryParser(tid2tree, module_aggregator.op_list_groupby_name)
        memory_parser.parse_events(self.events)
        self.memory_events = memory_parser.get_memory_events()
        self.memory_stats = memory_parser.get_memory_statistics()

        if self.has_kernel:
            logger.debug("KernelParser")
            kernel_parser = KernelParser()
            kernel_parser.parse_events(self.events)
            self.kernel_stat = kernel_parser.kernel_stat
            self.tc_used_ratio = kernel_parser.tc_used_ratio

    def analyze(self):
        self.recommendations = []

        dataloader_ratio = self.avg_costs.costs[ProfileRole.DataLoader] / self.avg_costs.costs[ProfileRole.Total]
        if dataloader_ratio > 0.05:
            text = "This run has high time cost on input data loading. " \
                   "{}% of the step time is in DataLoader. You could " \
                   "try to set num_workers on DataLoader's construction " \
                   "and enable multi-processes on data loading. " \
                   "Reference: <a href =\"{}\" target=\"_blank\">Single- and Multi-process Data Loading</a>".format(
                       round(dataloader_ratio * 100, 1),
                       "https://pytorch.org/docs/stable/data.html#single-and-multi-process-data-loading"
                   )
            self.recommendations.append(text)

        self._analyze_distributed_metrics()
        self._analyze_gpu_metrics()

    def _analyze_distributed_metrics(self):
        if self.use_dp and len(self.used_devices) > 1:
            text = "It is recommended to use DistributedDataParallel, instead of DataParallel to do multi-GPU training." \
                   "Reference: <a href = \"{}\" target=\"_blank\">Use DistributedDataParallel instead of DataParallel</a>".format(
                       "https://pytorch.org/docs/stable/notes/cuda.html#cuda-nn-ddp-instead"
                   )
            self.recommendations.append(text)

        if self.use_ddp and not self.use_nccl and self.device_props:
            for device_prop in self.device_props:
                major = device_prop.get("computeMajor")
                minor = device_prop.get("computeMinor")
                if major is None or minor is None:
                    continue
                compute_capability = "{}.{}".format(major, minor)
                if float(compute_capability) >= 3.5:
                    text = "Nccl backend is currently the fastest and highly recommended backend when using DDP for training."
                    self.recommendations.append(text)
                    break

        communication_ratio = self.avg_costs.costs[ProfileRole.Communication] / self.avg_costs.costs[ProfileRole.Total]
        if communication_ratio > 0.1:
            text = "This run has high time cost on communication. " \
                   "{}% of the step time is in communication. You could " \
                   "try Gradient Accumulation or increase the batch size. " \
                   "Note: Gradient accumulation will increase global effective batch size, which may hurt model convergence and accuracy. " \
                   "For such case, you may want to evaluate <a href = \"{}\" target=\"_blank\">LAMB optimizer</a>".format(
                       round(communication_ratio * 100, 1), "https://nvidia.github.io/apex/optimizers.html#apex.optimizers.FusedLAMB")
            self.recommendations.append(text)

    def _analyze_gpu_metrics(self):
        def get_gpus_str(gpus):
            gpu_list_str = str(gpus[0])
            for i in range(1, len(gpus)):
                if i == len(gpus) - 1:
                    gpu_list_str += "and {}".format(gpus[i])
                else:
                    gpu_list_str += ", {}".format(gpus[i])
            has_str = "has" if len(gpu_list_str) == 1 else "have"
            return gpu_list_str, has_str

        low_util_gpus = []
        for gpu_id in self.gpu_ids:
            if self.gpu_utilization[gpu_id] < 0.5:
                low_util_gpus.append(gpu_id)
        if len(low_util_gpus) > 0:
            gpu_list_str, has_str = get_gpus_str(low_util_gpus)
            text = "GPU {} {} low utilization. You could try to " \
                   "increase batch size to improve. Note: Increasing batch size " \
                   "may affect the speed and stability of model convergence.".format(
                gpu_list_str, has_str)
            self.recommendations.append(text)

class DistributedRunProfileData:
    def __init__(self, run_profile_data):
        self.worker = run_profile_data.worker
        self.span = run_profile_data.span
        self.steps_names = run_profile_data.steps_names
        self.has_communication = run_profile_data.has_communication
        self.comm_node_list = run_profile_data.comm_node_list
        self.comm_overlap_costs = run_profile_data.comm_overlap_costs
        self.used_devices = run_profile_data.used_devices
        self.device_props = run_profile_data.device_props
        self.distributed_info = run_profile_data.distributed_info

        self.total_comm_stats = None
        self.step_comm_stats = None

    def communication_parse(self):
        self.step_comm_stats, self.total_comm_stats = analyze_communication_nodes(self.comm_node_list)
