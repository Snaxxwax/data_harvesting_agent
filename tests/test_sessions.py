from harvest import sessions


def test_issued_cookie_verifies():
    token = "operator-secret-token-at-least-24-chars"
    cookie = sessions.issue(token)
    assert sessions.verify(token, cookie)


def test_cookie_does_not_verify_with_wrong_token():
    cookie = sessions.issue("token-one-at-least-24-characters")
    assert not sessions.verify("token-two-at-least-24-characters", cookie)


def test_cookie_expires():
    token = "operator-secret-token-at-least-24-chars"
    cookie = sessions.issue(token, now=1000)
    assert sessions.verify(token, cookie, now=1000 + sessions.SESSION_SECONDS - 1)
    assert not sessions.verify(token, cookie, now=1000 + sessions.SESSION_SECONDS + 1)


def test_garbage_cookie_rejected():
    token = "operator-secret-token-at-least-24-chars"
    assert not sessions.verify(token, None)
    assert not sessions.verify(token, "")
    assert not sessions.verify(token, "not-a-real-cookie")
    assert not sessions.verify(token, "abc.def")


def test_tampered_payload_rejected():
    token = "operator-secret-token-at-least-24-chars"
    cookie = sessions.issue(token)
    payload, _, mac = cookie.partition(".")
    tampered = sessions._b64(b"9999999999") + "." + mac
    assert not sessions.verify(token, tampered)
