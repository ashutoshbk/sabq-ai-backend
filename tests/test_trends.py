from tests.conftest import FakeResponse, trend_item, trends_json

U1 = "https://www.weforum.org/stories/2026/01/predictive-policing/"
U2 = "https://www.europol.europa.eu/publications/ai-in-law-enforcement"
U3 = "https://www.interpol.int/en/News-and-Events/digital-twins"
U4 = "https://www.oecd.org/en/publications/agile-policy.html"


def good_response(*urls, prefix="Trend", extra_cited=()):
    items = [trend_item(u, title=f"{prefix} {i}") for i, u in enumerate(urls, 1)]
    return FakeResponse(trends_json(*items), cited_urls=[*urls, *extra_cited])


def test_returns_three_verified_cards(api):
    client, fake = api
    fake.responses.queue.append(good_response(U1, U2, U3))

    r = client.get("/trends")

    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert [t["id"] for t in body["trends"]] == [1, 2, 3]
    assert body["cached"] is False
    assert body["ai_generated"] is True
    assert body["warning"] is None
    first = body["trends"][0]
    assert first["source_name"] == "World Economic Forum"  # from OUR list, not the model
    assert first["source_url"] == U1
    assert first["category"] == "Strategic Thinking"
    assert first["timeframe"] == "1-2 years"


def test_uses_web_search_with_allowed_domains(api):
    client, fake = api
    fake.responses.queue.append(good_response(U1, U2, U3))
    client.get("/trends")

    sent = fake.responses.calls[0]
    tool = sent["tools"][0]
    assert tool["type"] == "web_search"
    domains = tool["filters"]["allowed_domains"]
    assert "weforum.org" in domains and "interpol.int" in domains
    assert sent["tool_choice"] == "required"
    assert sent["store"] is False


def test_tracking_params_are_stripped(api):
    client, fake = api
    tracked = U1 + "?utm_source=openai"
    fake.responses.queue.append(
        FakeResponse(
            trends_json(trend_item(tracked, "A"), trend_item(U2, "B"), trend_item(U3, "C")),
            cited_urls=[tracked, U2, U3],
        )
    )
    body = client.get("/trends").json()
    assert body["trends"][0]["source_url"] == U1


def test_card_with_uncited_url_is_dropped(api):
    """The model invents a URL that the search never returned: it must not reach the user."""
    client, fake = api
    invented = "https://www.interpol.int/made-up-page"
    fake.responses.queue.append(
        FakeResponse(
            trends_json(trend_item(U1, "A"), trend_item(invented, "Fake"), trend_item(U2, "B"), trend_item(U3, "C")),
            cited_urls=[U1, U2, U3],
        )
    )
    body = client.get("/trends").json()
    assert body["count"] == 3
    assert all(t["title"] != "Fake" for t in body["trends"])


def test_card_from_disallowed_domain_is_dropped(api):
    client, fake = api
    bad = "https://random-blog.example.com/post"
    fake.responses.queue.append(
        FakeResponse(
            trends_json(trend_item(bad, "Blog"), trend_item(U1, "A"), trend_item(U2, "B"), trend_item(U3, "C")),
            cited_urls=[bad, U1, U2, U3],
        )
    )
    body = client.get("/trends").json()
    assert body["count"] == 3
    assert all("example.com" not in t["source_url"] for t in body["trends"])


def test_lookalike_domain_is_not_allowed(api):
    client, fake = api
    sneaky = "https://notweforum.org/page"
    fake.responses.queue.append(
        FakeResponse(trends_json(trend_item(sneaky, "Sneaky")), cited_urls=[sneaky])
    )
    fake.responses.queue.append(FakeResponse(trends_json(), cited_urls=[]))
    r = client.get("/trends")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "no_valid_trends"


def test_retries_once_when_fewer_than_three(api):
    client, fake = api
    fake.responses.queue.append(good_response(U1, U2))  # only 2
    fake.responses.queue.append(good_response(U3, U4, prefix="Other"))  # retry adds the third

    body = client.get("/trends").json()

    assert body["count"] == 3
    assert len(fake.responses.calls) == 2
    assert "Do not use these pages again" in fake.responses.calls[1]["input"]


def test_returns_partial_with_warning_if_still_short(api):
    client, fake = api
    fake.responses.queue.append(good_response(U1, U2))
    fake.responses.queue.append(good_response(U1, U2))  # retry finds only duplicates

    body = client.get("/trends").json()

    assert body["count"] == 2
    assert "Only 2 of 3" in body["warning"]


def test_duplicate_urls_are_not_counted_twice(api):
    client, fake = api
    fake.responses.queue.append(
        FakeResponse(
            trends_json(trend_item(U1, "A"), trend_item(U1 + "/", "A again"), trend_item(U2, "B"), trend_item(U3, "C")),
            cited_urls=[U1, U2, U3],
        )
    )
    body = client.get("/trends").json()
    assert body["count"] == 3
    assert len({t["source_url"].rstrip("/") for t in body["trends"]}) == 3


def test_invalid_category_and_timeframe_are_coerced(api):
    client, fake = api
    fake.responses.queue.append(
        FakeResponse(
            trends_json(
                trend_item(U1, "A", category="Cooking", timeframe="soon"),
                trend_item(U2, "B"),
                trend_item(U3, "C"),
            ),
            cited_urls=[U1, U2, U3],
        )
    )
    first = client.get("/trends").json()["trends"][0]
    assert first["category"] == "General"
    assert first["timeframe"] == "Not specified"


def test_second_call_is_served_from_cache(api):
    client, fake = api
    fake.responses.queue.append(good_response(U1, U2, U3))

    first = client.get("/trends").json()
    second = client.get("/trends").json()

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]
    assert len(fake.responses.calls) == 1


def test_refresh_bypasses_cache(api):
    client, fake = api
    fake.responses.queue.append(good_response(U1, U2, U3))
    fake.responses.queue.append(good_response(U4, U2, U3, prefix="Fresh"))

    client.get("/trends")
    refreshed = client.get("/trends?refresh=true").json()

    assert refreshed["cached"] is False
    assert refreshed["trends"][0]["source_url"] == U4
    assert len(fake.responses.calls) == 2


def test_model_returning_garbage_gives_clean_502(api):
    client, fake = api
    fake.responses.queue.append(FakeResponse("Sorry, I could not find anything."))
    fake.responses.queue.append(FakeResponse("Still nothing."))
    r = client.get("/trends")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "no_valid_trends"


def test_json_wrapped_in_code_fence_still_parses(api):
    client, fake = api
    text = "```json\n" + trends_json(trend_item(U1, "A"), trend_item(U2, "B"), trend_item(U3, "C")) + "\n```"
    fake.responses.queue.append(FakeResponse(text, cited_urls=[U1, U2, U3]))
    assert client.get("/trends").json()["count"] == 3
