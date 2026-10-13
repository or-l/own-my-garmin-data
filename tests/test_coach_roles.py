from coach_roles import CoachLoopRoles, generate_password, role_of_url, url_for_role


def test_url_for_role_swaps_credentials_and_keeps_host_and_query():
    owner = "postgresql://owner:secret@ep-x.eu.aws.neon.tech/neondb?sslmode=require"
    url = url_for_role(owner, "coach", "p@ss/word")
    assert url == "postgresql://coach:p%40ss%2Fword@ep-x.eu.aws.neon.tech/neondb?sslmode=require"
    assert role_of_url(url) == "coach"


def test_role_of_url_handles_missing():
    assert role_of_url(None) is None
    assert role_of_url("postgresql://host/db") is None


def test_generated_passwords_are_long_and_distinct():
    a, b = generate_password(), generate_password()
    assert a != b and len(a) >= 40


def test_roles_config_round_trip():
    r = CoachLoopRoles(coach="c", scheduler="s", read="bk_reader")
    assert CoachLoopRoles.from_config(r.to_config()) == r
    assert CoachLoopRoles.from_config({}) == CoachLoopRoles()
