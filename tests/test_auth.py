import pytest
from httpx import AsyncClient


async def test_signup_creates_user_and_returns_tokens(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/signup",
        json={
            "email": "New.User@Example.com",
            "password": "Str0ngPass",
            "full_name": "New User",
            "phone": "+919876543210",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["email"] == "new.user@example.com"
    assert body["user"]["is_admin"] is False
    assert body["tokens"]["token_type"] == "bearer"
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]


async def test_signup_rejects_duplicate_email(client: AsyncClient, user) -> None:
    response = await client.post(
        "/auth/signup",
        json={"email": user.email, "password": "Str0ngPass", "full_name": "Duplicate"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"


@pytest.mark.parametrize(
    "payload,expected_code",
    [
        ({"email": "not-an-email", "password": "Str0ngPass", "full_name": "X"}, "validation_error"),
        (
            {"email": "weak@example.com", "password": "short", "full_name": "Weak"},
            "validation_error",
        ),
        (
            {"email": "nodigit@example.com", "password": "onlyletters", "full_name": "No Digit"},
            "validation_error",
        ),
        (
            {"email": "blank@example.com", "password": "Str0ngPass", "full_name": "  "},
            "validation_error",
        ),
        ({"email": "nopassword@example.com", "full_name": "No Password"}, "validation_error"),
    ],
)
async def test_signup_validation(client: AsyncClient, payload, expected_code) -> None:
    response = await client.post("/auth/signup", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == expected_code


async def test_login_with_valid_credentials(client: AsyncClient, user) -> None:
    response = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(user.id)


async def test_login_with_wrong_password_is_rejected(client: AsyncClient, user) -> None:
    response = await client.post(
        "/auth/login", json={"email": user.email, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_for_unknown_email_returns_same_error(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "Passw0rd!"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_me_requires_token(client: AsyncClient) -> None:
    response = await client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"


async def test_me_rejects_garbage_token(client: AsyncClient) -> None:
    response = await client.get("/auth/me", headers={"Authorization": "Bearer not.a.token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_me_rejects_refresh_token_as_access_token(client: AsyncClient, user) -> None:
    login = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    refresh_token = login.json()["tokens"]["refresh_token"]
    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {refresh_token}"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "wrong_token_type"


async def test_me_returns_current_user(client: AsyncClient, auth_headers, user) -> None:
    response = await client.get("/auth/me", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["email"] == user.email


async def test_refresh_rotates_and_invalidates_old_token(client: AsyncClient, user) -> None:
    login = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    original = login.json()["tokens"]["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": original})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]
    assert rotated != original

    # Replaying a token that was rotated past is the theft signal, and is
    # reported distinctly from a plain revocation.
    replay = await client.post("/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reuse_detected"

    rotated_access = first.json()["access_token"]
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {rotated_access}"})
    assert me.status_code == 200


async def test_reusing_a_rotated_token_revokes_the_whole_chain(
    client: AsyncClient, user
) -> None:
    login = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    original = login.json()["tokens"]["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": original})
    assert first.status_code == 200
    successor = first.json()["refresh_token"]

    replay = await client.post("/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reuse_detected"

    # The successor is collateral damage: once a superseded token comes back the
    # whole chain is untrustworthy, so the honest user is signed out too.
    after = await client.post("/auth/refresh", json={"refresh_token": successor})
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "refresh_token_revoked"


async def test_reuse_detection_is_scoped_to_one_session(client: AsyncClient, user) -> None:
    first_login = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    second_login = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    compromised = first_login.json()["tokens"]["refresh_token"]
    unrelated = second_login.json()["tokens"]["refresh_token"]

    rotated = await client.post("/auth/refresh", json={"refresh_token": compromised})
    assert rotated.status_code == 200
    replay = await client.post("/auth/refresh", json={"refresh_token": compromised})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_reuse_detected"

    # A separate login is a separate family and must survive the burn.
    survivor = await client.post("/auth/refresh", json={"refresh_token": unrelated})
    assert survivor.status_code == 200, survivor.text


async def test_logout_revokes_refresh_token(client: AsyncClient, user) -> None:
    login = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    token = login.json()["tokens"]["refresh_token"]
    assert (await client.post("/auth/logout", json={"refresh_token": token})).status_code == 200

    # A token revoked by an explicit logout has no successor, so replaying it
    # stays a plain revocation and does not escalate to reuse detection.
    replay = await client.post("/auth/refresh", json={"refresh_token": token})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "refresh_token_revoked"


async def test_inactive_user_cannot_login(client: AsyncClient, user) -> None:
    user.is_active = False
    response = await client.post(
        "/auth/login", json={"email": user.email, "password": "Passw0rd!"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "account_disabled"


async def test_oauth2_password_form_login(client: AsyncClient, user) -> None:
    response = await client.post(
        "/auth/token", data={"username": user.email, "password": "Passw0rd!"}
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_token_expiry_matches_access_token_lifetime(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/signup",
        json={"email": "expiry@example.com", "password": "Str0ngPass", "full_name": "Expiry"},
    )
    assert response.status_code == 201, response.text
    from app.core.config import settings

    expected = settings.access_token_expire_minutes * 60
    assert response.json()["tokens"]["expires_in"] == expected
