from datetime import datetime, timezone

import pytest
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.core.models.retrieval import SearchRequest
from assistant_rh_api.db.auth_stores import GroupStore, SessionStore
from assistant_rh_api.db.content_store import ContentStore
from assistant_rh_api.db.feedback_store import FeedbackStore
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.db.search_store import SearchStore
from assistant_rh_api.db.settings_stores import AcronymStore, ConfigStore, PromptStore

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "call",
    [
        lambda db: ConfigStore(db).load(),
        lambda db: PromptStore(db).get("system"),
        lambda db: AcronymStore(db).load(),
        lambda db: GroupStore(db).get("group"),
        lambda db: SessionStore(db).get_active("a" * 64, datetime.now(timezone.utc)),
        lambda db: ContentStore(db).documents(("00000000-0000-0000-0000-000000000001",)),
        lambda db: SearchStore(db).search(SearchRequest("matte", "lexical", query="congés")),
        lambda db: ChatRunStore(db).get("run"),
        lambda db: FeedbackStore(db).get("run"),
    ],
)
async def test_each_repository_distinguishes_unavailable_from_absence(repository_db, call):
    await repository_db.close()
    with pytest.raises(DatabaseUnavailable) as caught:
        await call(repository_db)
    assert str(caught.value) == "database_unavailable"
