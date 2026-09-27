import os
from collections.abc import AsyncGenerator

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("RATE_LIMIT_REQUESTS", "0")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-that-is-long-enough-for-hmac-sha256")
os.environ.setdefault("WEBHOOK_REQUIRE_SIGNATURE", "false")
os.environ.setdefault("WEBHOOK_SIGNING_SECRET", "test-webhook-secret-value-long-enough")

import pytest
import pytest_asyncio
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import (
    CentreTest,
    DiagnosticCentre,
    DiagnosticTest,
    User,
)
from app.services.cache import NullCache, set_cache
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

SQLITE_URL = "sqlite+aiosqlite:///:memory:"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def _engine_for(url: str):
    if url.startswith("sqlite"):
        return create_async_engine(
            url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    return create_async_engine(url, pool_pre_ping=True)


@pytest.fixture(scope="session", autouse=True)
def _disable_cache() -> None:
    set_cache(NullCache())


@pytest_asyncio.fixture
async def engine():
    test_engine = _engine_for(TEST_DATABASE_URL or SQLITE_URL)
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield test_engine
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
        await db_session.rollback()


@pytest_asyncio.fixture
async def concurrent_client(engine) -> AsyncGenerator[AsyncClient, None]:
    """Client whose requests each get their own session, like production.

    A single shared session cannot serve concurrent requests: SQLAlchemy
    forbids parallel operations on one session, and on SQLite every task would
    silently share one connection anyway. This fixture is what makes the
    concurrency tests meaningful against a real database.
    """
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        async with factory() as request_session:
            try:
                yield request_session
            except Exception:
                await request_session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield http_client
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(engine, session) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_session():
        yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
    app.dependency_overrides.clear()


async def _persist_user(
    session: AsyncSession, *, email: str, password: str = "Passw0rd!", admin: bool = False
) -> User:
    user = User(
        email=email,
        full_name=email.split("@")[0].title(),
        hashed_password=hash_password(password),
        is_admin=admin,
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def user_factory(session: AsyncSession):
    async def factory(
        email: str = "user@example.com", password: str = "Passw0rd!", admin: bool = False
    ) -> User:
        return await _persist_user(session, email=email, password=password, admin=admin)

    return factory


@pytest_asyncio.fixture
async def user(session: AsyncSession) -> User:
    return await _persist_user(session, email="user@example.com")


@pytest_asyncio.fixture
async def other_user(session: AsyncSession) -> User:
    return await _persist_user(session, email="other@example.com")


@pytest_asyncio.fixture
async def admin_user(session: AsyncSession) -> User:
    return await _persist_user(session, email="admin@eve.health", admin=True)


@pytest_asyncio.fixture
async def auth_headers(client: AsyncClient, user: User) -> dict[str, str]:
    response = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['tokens']['access_token']}"}


@pytest_asyncio.fixture
async def other_auth_headers(client: AsyncClient, other_user: User) -> dict[str, str]:
    response = await client.post(
        "/auth/login", json={"email": other_user.email, "password": "Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['tokens']['access_token']}"}


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient, admin_user: User) -> dict[str, str]:
    response = await client.post(
        "/auth/login", json={"email": admin_user.email, "password": "Passw0rd!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['tokens']['access_token']}"}


@pytest_asyncio.fixture
async def catalogue(session: AsyncSession) -> dict:
    centre = DiagnosticCentre(
        name="EVE City Diagnostics",
        address="12 MG Road, Bengaluru, KA 560001",
        city="Bengaluru",
        state="Karnataka",
        postal_code="560001",
        phone="+918000000001",
        latitude=12.971599,
        longitude=77.594566,
    )
    cbc_test = DiagnosticTest(
        code="CBC",
        name="Complete Blood Count",
        description="Haemoglobin, WBC and platelet counts.",
        duration_minutes=30,
        base_price=450,
        fasting_required=False,
    )
    lipid_test = DiagnosticTest(
        code="LIPID",
        name="Lipid Profile",
        description="Cholesterol and triglyceride panel.",
        duration_minutes=45,
        base_price=900,
        fasting_required=True,
    )
    other_centre = DiagnosticCentre(
        name="EVE Airport Diagnostics",
        address="4 Airport Road, Bengaluru, KA 560017",
        city="Bengaluru",
        state="Karnataka",
    )
    session.add_all([centre, cbc_test, lipid_test, other_centre])
    await session.flush()

    session.add_all(
        [
            CentreTest(centre_id=centre.id, test_id=cbc_test.id, price=500),
            CentreTest(centre_id=centre.id, test_id=lipid_test.id, price=950),
        ]
    )
    await session.flush()
    await session.commit()
    return {
        "centre": centre,
        "other_centre": other_centre,
        "cbc": cbc_test,
        "lipid": lipid_test,
    }
