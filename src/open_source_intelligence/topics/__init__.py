"""Topic configuration: the read queries and the create/update/delete operations.

The package exists because the contract said it should. `docs/api-contract.md` §3 recorded the
absence of a topic API as "designed but not done, and whoever adds one should put the contract in
`INTERFACES.md` §3 first", which is what T19 did. What stayed out is the part the contract argues
against on its own merits: triggering a research run is not reachable from here, because a run
spends model budget and that trigger belongs to the scheduler and the operator CLI.
"""

from .errors import TopicError, TopicInUse, TopicNameBlank, TopicNameConflict, TopicNotFound
from .queries import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, TopicPage, clamp_page_size, list_topics
from .service import (
    FIELD_COLUMNS,
    REFERENCE_TABLES,
    create_topic,
    delete_topic,
    normalize_name,
    reference_counts,
    require_usable_name,
    update_topic,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "FIELD_COLUMNS",
    "MAX_PAGE_SIZE",
    "REFERENCE_TABLES",
    "TopicError",
    "TopicInUse",
    "TopicNameBlank",
    "TopicNameConflict",
    "TopicNotFound",
    "TopicPage",
    "clamp_page_size",
    "create_topic",
    "delete_topic",
    "list_topics",
    "normalize_name",
    "reference_counts",
    "require_usable_name",
    "update_topic",
]
