"""Minimal durable Microsoft Agent Framework workflow spike.

The workflow contains no model call. It deliberately isolates the durability
mechanism: Agent Framework Workflow -> Durable Extension -> Durable Task worker
-> Durable Task Scheduler.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, cast

from agent_framework import (
    Executor,
    Workflow,
    WorkflowBuilder,
    WorkflowContext,
    handler,
    response_handler,
)
from agent_framework_durabletask import (
    DurableAIAgentWorker,
    DurableWorkflowClient,
    deserialize_workflow_output,
)
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from durabletask import history as durable_history
from durabletask.azuremanaged.client import DurableTaskSchedulerClient
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker
from durabletask.worker import (
    VersionFailureStrategy,
    VersioningOptions,
    VersionMatchStrategy,
)
from pydantic import BaseModel
from typing_extensions import Never


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("durable-spike")

WORKFLOW_NAME = "durable_signal_spike"
LOCAL_ENDPOINT = "http://localhost:8080"
DEFAULT_WORKFLOW_VERSION = "1.0.0"
WORKFLOW_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")


@dataclass
class StartRequest:
    business_key: str
    input_value: int


@dataclass
class PreparedWork:
    business_key: str
    input_value: int
    deterministic_value: int


class ApprovalSignal(BaseModel):
    signal_id: str
    approved: bool
    note: str = ""


@dataclass
class ApprovedWork:
    business_key: str
    deterministic_value: int
    signal_id: str
    approved: bool
    note: str


@dataclass
class CompletionRecord:
    business_key: str
    deterministic_value: int
    signal_id: str
    outcome: str
    finalize_marker: str


class PrepareExecutor(Executor):
    def __init__(self) -> None:
        super().__init__(id="prepare")

    @handler
    async def prepare(self, request: StartRequest, ctx: WorkflowContext[PreparedWork]) -> None:
        # Pure deterministic work. For input 21, the persisted result is 42.
        prepared = PreparedWork(
            business_key=request.business_key,
            input_value=request.input_value,
            deterministic_value=request.input_value * 2,
        )
        logger.info("Prepared %s -> %s", request.business_key, prepared.deterministic_value)
        await ctx.send_message(prepared)


class ApprovalGateExecutor(Executor):
    def __init__(self) -> None:
        super().__init__(id="approval_gate")

    @handler
    async def request_approval(self, work: PreparedWork, ctx: WorkflowContext) -> None:
        # Keep the request JSON-native. agent-framework-durabletask 1.0.0b260709
        # serializes the already-serialized original request once more when it
        # routes a HITL response into the activity. A custom dataclass request
        # therefore reappears as its checkpoint-marker dict after one decode and
        # cannot match a typed @response_handler after a worker restart. A plain
        # dict survives that extra round trip unchanged.
        request = {
            "business_key": work.business_key,
            "deterministic_value": work.deterministic_value,
            "prompt": f"Approve durable continuation for {work.business_key}?",
        }
        await ctx.request_info(request_data=request, response_type=ApprovalSignal)

    @response_handler(request=dict, response=ApprovalSignal, output=ApprovedWork)
    async def receive_approval(
        self,
        original_request,
        response,
        ctx,
    ) -> None:
        request_data = cast(dict[str, Any], original_request)
        approval = cast(ApprovalSignal, response)
        logger.info(
            "Received signal %s for %s (approved=%s)",
            approval.signal_id,
            request_data["business_key"],
            approval.approved,
        )
        await ctx.send_message(
            ApprovedWork(
                business_key=str(request_data["business_key"]),
                deterministic_value=int(request_data["deterministic_value"]),
                signal_id=approval.signal_id,
                approved=approval.approved,
                note=approval.note,
            )
        )


class FinalizeExecutor(Executor):
    def __init__(self) -> None:
        super().__init__(id="finalize")

    @handler
    async def finalize(self, work: ApprovedWork, ctx: WorkflowContext[Never, CompletionRecord]) -> None:
        # This marker lets the history/output test count logical finalization.
        # It is not an external side effect and does not pretend to be exactly-once.
        record = CompletionRecord(
            business_key=work.business_key,
            deterministic_value=work.deterministic_value,
            signal_id=work.signal_id,
            outcome="approved" if work.approved else "rejected",
            finalize_marker=f"finalized:{work.business_key}:{work.signal_id}",
        )
        logger.info("Finalized %s", record.finalize_marker)
        await ctx.yield_output(record)


def create_workflow() -> Workflow:
    prepare = PrepareExecutor()
    approval = ApprovalGateExecutor()
    finalize = FinalizeExecutor()
    return (
        WorkflowBuilder(name=WORKFLOW_NAME, start_executor=prepare)
        .add_edge(prepare, approval)
        .add_edge(approval, finalize)
        .build()
    )


def workflow_version() -> str:
    version = os.getenv("WORKFLOW_VERSION", DEFAULT_WORKFLOW_VERSION)
    if not WORKFLOW_VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "WORKFLOW_VERSION must contain one to three dot-separated integer "
            f"components (for example, '1.0.0'); got {version!r}"
        )
    return version


def settings() -> tuple[str, str, str]:
    return (
        os.getenv("ENDPOINT", LOCAL_ENDPOINT),
        os.getenv("TASKHUB", "default"),
        workflow_version(),
    )


def credential_for(endpoint: str):
    if endpoint == LOCAL_ENDPOINT:
        return None
    managed_identity_client_id = os.getenv("AZURE_MANAGED_IDENTITY_CLIENT_ID")
    if managed_identity_client_id:
        return ManagedIdentityCredential(client_id=managed_identity_client_id)
    return AzureCliCredential()


def make_scheduler_client() -> DurableTaskSchedulerClient:
    endpoint, taskhub, version = settings()
    return DurableTaskSchedulerClient(
        host_address=endpoint,
        secure_channel=endpoint != LOCAL_ENDPOINT,
        taskhub=taskhub,
        token_credential=credential_for(endpoint),
        default_version=version,
    )


def make_workflow_client() -> tuple[DurableTaskSchedulerClient, DurableWorkflowClient]:
    scheduler = make_scheduler_client()
    return scheduler, DurableWorkflowClient(scheduler, workflow_name=WORKFLOW_NAME)


def jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def print_json(value: Any) -> None:
    print(json.dumps(jsonable(value), indent=2, sort_keys=True, default=str))


async def run_worker() -> None:
    endpoint, taskhub, version = settings()
    scheduler_worker = DurableTaskSchedulerWorker(
        host_address=endpoint,
        secure_channel=endpoint != LOCAL_ENDPOINT,
        taskhub=taskhub,
        token_credential=credential_for(endpoint),
    )
    scheduler_worker.use_versioning(
        VersioningOptions(
            version=version,
            default_version=version,
            match_strategy=VersionMatchStrategy.STRICT,
            failure_strategy=VersionFailureStrategy.REJECT,
        )
    )
    durable_worker = DurableAIAgentWorker(scheduler_worker)
    workflow = create_workflow()
    durable_worker.configure_workflow(workflow)
    logger.info(
        "Worker ready: endpoint=%s taskhub=%s workflow=%s version=%s",
        endpoint,
        taskhub,
        WORKFLOW_NAME,
        version,
    )
    scheduler_worker.start()
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        scheduler_worker.stop()


def command_start(args: argparse.Namespace) -> None:
    scheduler, workflow = make_workflow_client()
    try:
        instance_id = workflow.start_workflow(
            input={"business_key": args.business_key, "input_value": args.input_value},
            instance_id=args.instance_id,
        )
        print_json({"instance_id": instance_id, "workflow": WORKFLOW_NAME})
    finally:
        scheduler.close()


def wait_for_pending(
    workflow: DurableWorkflowClient, instance_id: str, timeout_seconds: int
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pending = workflow.get_pending_hitl_requests(instance_id)
        if pending:
            return pending
        status = workflow.get_runtime_status(instance_id)
        if status in {"COMPLETED", "FAILED", "TERMINATED"}:
            raise RuntimeError(f"Instance became {status} before exposing a pending request")
        time.sleep(1)
    raise TimeoutError(f"No pending request appeared within {timeout_seconds} seconds")


def command_pending(args: argparse.Namespace) -> None:
    scheduler, workflow = make_workflow_client()
    try:
        pending = wait_for_pending(workflow, args.instance_id, args.timeout)
        print_json(pending)
    finally:
        scheduler.close()


def command_signal(args: argparse.Namespace) -> None:
    scheduler, workflow = make_workflow_client()
    payload = {
        "signal_id": args.signal_id,
        "approved": args.decision == "approve",
        "note": args.note,
    }
    deliveries: list[dict[str, Any]] = []
    try:
        for delivery_number in range(1, args.repeat + 1):
            try:
                workflow.send_hitl_response(
                    args.instance_id,
                    args.request_id,
                    payload,
                )
                deliveries.append({"delivery": delivery_number, "accepted_by_client_call": True})
            except Exception as exc:  # Preserve terminal/duplicate behavior as test evidence.
                deliveries.append(
                    {
                        "delivery": delivery_number,
                        "accepted_by_client_call": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
        print_json({"payload": payload, "deliveries": deliveries})
    finally:
        scheduler.close()


def state_to_dict(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    custom_status = None
    if state.serialized_custom_status:
        try:
            custom_status = json.loads(state.serialized_custom_status)
        except json.JSONDecodeError:
            custom_status = state.serialized_custom_status
    output = None
    if state.serialized_output:
        try:
            output = json.loads(state.serialized_output)
        except json.JSONDecodeError:
            output = state.serialized_output
    return {
        "instance_id": state.instance_id,
        "name": state.name,
        "runtime_status": state.runtime_status.name,
        "created_at": state.created_at,
        "last_updated_at": state.last_updated_at,
        "input": state.get_input(),
        "output": output,
        "custom_status": custom_status,
        "failure_details": str(state.failure_details) if state.failure_details else None,
    }


def command_status(args: argparse.Namespace) -> None:
    scheduler = make_scheduler_client()
    try:
        endpoint, taskhub, version = settings()
        print_json(
            {
                "query": {
                    "endpoint": endpoint,
                    "taskhub": taskhub,
                    "workflow": WORKFLOW_NAME,
                    "expected_orchestration_name": f"dafx-{WORKFLOW_NAME}",
                    "version": version,
                },
                "state": state_to_dict(
                    scheduler.get_orchestration_state(args.instance_id)
                ),
            }
        )
    finally:
        scheduler.close()


def command_wait(args: argparse.Namespace) -> None:
    scheduler = make_scheduler_client()
    try:
        # Do not use DurableWorkflowClient.await_workflow_output here. In some
        # DTS/emulator versions its server-streaming wait RPC can return
        # DEADLINE_EXCEEDED immediately instead of honoring the requested
        # timeout. Polling persisted orchestration state exercises the same
        # durability boundary and gives materially better diagnostics.
        deadline = time.monotonic() + args.timeout
        last_status: str | None = None
        while time.monotonic() < deadline:
            state = scheduler.get_orchestration_state(args.instance_id)
            if state is None:
                endpoint, taskhub, _ = settings()
                raise RuntimeError(
                    f"Workflow instance '{args.instance_id}' was not found at "
                    f"endpoint='{endpoint}', taskhub='{taskhub}'. Confirm that every "
                    "terminal uses the same ENDPOINT and TASKHUB and that the DTS "
                    "emulator was not stopped or recreated."
                )
            expected_name = f"dafx-{WORKFLOW_NAME}"
            if not isinstance(state.name, str) or state.name.casefold() != expected_name.casefold():
                raise RuntimeError(
                    f"Instance '{args.instance_id}' exists, but its orchestration name "
                    f"is '{state.name}', not expected '{expected_name}'. Use the instance "
                    "ID returned by the current spike's start command, or start a fresh "
                    "test instance ID."
                )
            status = state.runtime_status.name
            if status != last_status:
                logger.info("Workflow %s status=%s", args.instance_id, status)
                last_status = status
            if status == "COMPLETED":
                output = None
                if state.serialized_output is not None:
                    output = deserialize_workflow_output(json.loads(state.serialized_output))
                print_json({"instance_id": args.instance_id, "output": output})
                return
            if status in {"FAILED", "TERMINATED"}:
                details = state_to_dict(state)
                raise RuntimeError(
                    f"Workflow '{args.instance_id}' ended with status {status}: "
                    f"{json.dumps(details, default=str)}"
                )
            time.sleep(1)
        raise TimeoutError(
            f"Workflow '{args.instance_id}' did not complete within {args.timeout} seconds; "
            f"last status was {last_status}"
        )
    finally:
        scheduler.close()


def command_history(args: argparse.Namespace) -> None:
    scheduler = make_scheduler_client()
    try:
        state = scheduler.get_orchestration_state(args.instance_id)
        history = scheduler.get_orchestration_history(args.instance_id)
        events = [
            {"event_type": type(event).__name__, **event.to_dict()}
            for event in history
        ]
        raised = [
            event for event in history if isinstance(event, durable_history.EventRaisedEvent)
        ]
        scheduled = [
            event for event in history if isinstance(event, durable_history.TaskScheduledEvent)
        ]
        execution_started = next(
            (
                event
                for event in history
                if isinstance(event, durable_history.ExecutionStartedEvent)
            ),
            None,
        )
        summary = {
            "event_count": len(events),
            "execution_version": execution_started.version if execution_started else None,
            "external_event_count": len(raised),
            "external_event_names": [event.name for event in raised],
            "scheduled_activity_count": len(scheduled),
            "scheduled_activity_names": [event.name for event in scheduled],
            "finalize_activity_schedule_count": sum(
                1 for event in scheduled if "finalize" in event.name.casefold()
            ),
        }
        print_json({"state": state_to_dict(state), "summary": summary, "events": events})
    finally:
        scheduler.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("worker", help="Run the standalone Durable Task worker")

    start = subparsers.add_parser("start", help="Start a workflow instance")
    start.add_argument("--business-key", default="case-001")
    start.add_argument("--input-value", type=int, default=21)
    start.add_argument("--instance-id")

    pending = subparsers.add_parser("pending", help="Wait for and print pending HITL requests")
    pending.add_argument("instance_id")
    pending.add_argument("--timeout", type=int, default=60)

    signal = subparsers.add_parser("signal", help="Send one or more identical external signals")
    signal.add_argument("instance_id")
    signal.add_argument("request_id")
    signal.add_argument("--signal-id", default="external-signal-001")
    signal.add_argument("--decision", choices=["approve", "reject"], default="approve")
    signal.add_argument("--note", default="durability spike")
    signal.add_argument("--repeat", type=int, default=1)

    status = subparsers.add_parser("status", help="Query current orchestration state")
    status.add_argument("instance_id")

    wait = subparsers.add_parser("wait", help="Wait for and print workflow output")
    wait.add_argument("instance_id")
    wait.add_argument("--timeout", type=int, default=120)

    history = subparsers.add_parser("history", help="Print persisted orchestration history")
    history.add_argument("instance_id")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "worker":
        asyncio.run(run_worker())
    elif args.command == "start":
        command_start(args)
    elif args.command == "pending":
        command_pending(args)
    elif args.command == "signal":
        command_signal(args)
    elif args.command == "status":
        command_status(args)
    elif args.command == "wait":
        command_wait(args)
    elif args.command == "history":
        command_history(args)
    else:  # pragma: no cover
        raise AssertionError(args.command)


if __name__ == "__main__":
    main()
