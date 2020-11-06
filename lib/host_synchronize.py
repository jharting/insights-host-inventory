from datetime import timedelta

from sqlalchemy.orm.base import instance_state

from app.culling import _Config as CullingConfig
from app.culling import Timestamps
from app.queue.event_producer import Topic
from app.queue.events import build_event
from app.queue.events import EventType
from app.queue.events import message_headers
from app.queue.queue import EGRESS_HOST_FIELDS
from app.serialization import serialize_host
from lib.metrics import synchronize_host_count

__all__ = ("synchronize_hosts",)

WARN_DAYS_AFTER = 7
DELETE_DAYS_AFTER = 14


def synchronize_hosts(select_query, event_producer, chunk_size, interrupt=lambda: False):
    start = 0
    print("Total number: {}".format(select_query.count()))
    while select_query.offset(start).limit(chunk_size).count():
        host_list = select_query.offset(start).limit(chunk_size)
        for host in host_list:
            sync_up = _should_synchronize(host)
            if sync_up:
                serialized_host = serialize_host(host, _staleness_timestamps(), EGRESS_HOST_FIELDS)
                event = build_event(EventType.updated, serialized_host)
                insights_id = host.canonical_facts.get("insights_id")
                headers = message_headers(EventType.updated, insights_id)
                # incase of a failed update event, event_producer logs the message.
                event_producer.write_event(event, str(serialized_host), headers, Topic.events, wait=True)
                synchronize_host_count.inc()

            yield host.id, sync_up
        start += chunk_size

        # forced stop if needed.
        if interrupt():
            return


def _should_synchronize(host):
    # TODO: Check if this is still applicable when hosts are simply read from
    #
    # DB and NOT deleted by this session.
    # This process of checking for an already deleted host relies
    # on checking the session after it has been updated by the commit()
    # function and marked the deleted hosts as expired.  It is after this
    # change that the host is called by a new query and, if deleted by a
    # different process, triggers the ObjectDeletedError and is not emited.
    return not instance_state(host).expired


def _staleness_timestamps():
    cullingConfig = CullingConfig(
        stale_warning_offset_delta=timedelta(days=WARN_DAYS_AFTER),
        culled_offset_delta=timedelta(days=DELETE_DAYS_AFTER),
    )
    return Timestamps(cullingConfig)
