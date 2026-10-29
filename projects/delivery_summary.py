"""How many delivery challans a site has, by status (DC-B1). Read-only.

TWO SPELLINGS OF ONE COUNT, for the two screens that show it:

    site_challan_counts(challans)           from rows the caller already holds — the
                                            project overview, whose DC card has loaded
                                            every challan on the site anyway (0 queries)
    site_challan_counts_for_project(project) one aggregate query — the DC detail page,
                                            which holds only the one challan it shows

Both return the same dict, so the two screens cannot word the same site differently:

    {'total', 'received', 'partially_received', 'expected', 'rejected'}

`total` COUNTS EVERY ROW, not the sum of the four status keys. A status added to
DeliveryChallan later is therefore still counted in the total (and visible as a gap
between the total and its parts) rather than silently vanishing from the site's count.

Statuses are compared against DeliveryChallan's own constants, never string literals,
so a renamed status value cannot detach this count from the model.
"""
from django.db.models import Count, Q

from .models import DeliveryChallan

# Status constant -> dict key. One table, read by both functions, so the pure count and
# the aggregate cannot disagree about which status lands in which key.
_STATUS_KEYS = (
    (DeliveryChallan.RECEIVED,           'received'),
    (DeliveryChallan.PARTIALLY_RECEIVED, 'partially_received'),
    (DeliveryChallan.EXPECTED,           'expected'),
    (DeliveryChallan.REJECTED,           'rejected'),
)


def site_challan_counts(challans):
    """Count `challans` (an iterable of DeliveryChallan rows) by status. Pure: reads
    only `.status` on each row and runs no query, so a caller holding an evaluated list
    pays nothing. The caller decides which rows are the site's."""
    counts = {key: 0 for _status, key in _STATUS_KEYS}
    counts['total'] = 0
    by_status = dict(_STATUS_KEYS)
    for challan in challans:
        counts['total'] += 1
        key = by_status.get(challan.status)
        if key is not None:
            counts[key] += 1
    return counts


def site_challan_counts_for_project(project):
    """The same dict for every challan on `project`, in ONE aggregate query.

    Count(filter=Q) per status in a single SELECT rather than a values('status')
    group-by: the result is one row with every key present, zeros included, so there
    is no missing-key case to fill in afterwards. No soft-delete term: challans have no
    is_deleted of their own, and the caller has already resolved `project` through
    _active_project(), which refuses a deleted site.
    """
    return DeliveryChallan.objects.filter(project=project).aggregate(
        total=Count('pk'),
        **{key: Count('pk', filter=Q(status=status)) for status, key in _STATUS_KEYS},
    )
