"""Hook for the metrics the cards will grow later.

Deliberately a no-op in v1.  It is wired into the listing path so that adding
either follow-up is a change to this one function; ``_card.html`` already
renders ``cpu_percent``, ``mem_percent`` and ``kubelet`` only when they are not
``None``, so nothing else has to move.

Live CPU and memory
    ``NodeManager`` already issues ``getAllDomainStats`` once per listing (see
    ``libvirtctl.DOMAIN_STATS``), so adding ``VIR_DOMAIN_STATS_CPU_TOTAL`` to
    that flag set costs no extra round-trip and the figures arrive alongside
    the ones the cards already show.  CPU percentage is the delta of
    ``cpu.time`` (nanoseconds) between two samples divided by the elapsed wall
    time and the vCPU count, so this module needs to keep the previous sample.
    Memory is ``1 - balloon.usable / balloon.current``; both keys are optional,
    so use ``.get`` — the test driver reports neither.  The domains already
    carry ``<memballoon model='virtio'/>`` for exactly this.

Real kubelet status
    Add a kubeconfig path to ``[cluster]``, then ``GET /api/v1/nodes`` and map
    each node's ``Ready`` condition together with ``spec.unschedulable`` onto
    ``Ready`` / ``NotReady`` / ``SchedulingDisabled``.  This is the only thing
    entitled to say a node is *ready*: a ``configured`` state means firstboot
    enabled the unit, nothing more.
"""

from __future__ import annotations

from .conn import ConnectionManager
from .libvirtctl import Node


def enrich(cm: ConnectionManager, nodes: list[Node]) -> list[Node]:
    """Return *nodes* with metrics attached.  Currently attaches nothing."""
    return nodes
