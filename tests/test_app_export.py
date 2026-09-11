"""On-demand credential download and copy through the real HTTP application."""

from html.parser import HTMLParser

import pytest
import yaml

from .export_support import (
    FAILURES,
    SECRET,
    assert_guest_read,
    candidate_cluster as candidate_cluster,
    config_values as config_values,
    expected_document,
    export_agent as export_agent,
    inventory,
    script_failure,
)


class Page(HTMLParser):
    """Record element ancestry and decoded textarea content without an HTML dependency."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.textarea = []
        self.scripts = []
        self.text = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        element = (tag, dict(attrs))
        self.elements.append((element, tuple(self.stack)))
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.text.append(data)
        if any(tag == "script" for tag, _ in self.stack):
            self.scripts.append(data)
        if any(
            tag == "textarea" and attrs.get("id") == "kubeconfig-text" for tag, attrs in self.stack
        ):
            self.textarea.append(data)


@pytest.mark.parametrize("action", ["download", "copy"])
def test_export_is_on_demand_preserves_credentials_and_does_not_mutate_nodes(
    client, candidate_cluster, export_agent, conn, caplog, action
):
    before = inventory(conn)
    with caplog.at_level("DEBUG"):
        for path in ("/", "/nodes"):
            initial = client.get(path)
            assert initial.status_code == 200
            assert SECRET not in initial.text
        assert not export_agent.calls

        response = client.post(
            f"/kubeconfig/{action}",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        if action == "download":
            assert response.headers["content-type"].split(";")[0] in {
                "application/yaml",
                "application/x-yaml",
                "text/yaml",
                "text/plain",
            }
            assert response.headers["content-disposition"] == 'attachment; filename="k3s-demo.yaml"'
            text = response.text
        else:
            assert response.headers["content-type"].startswith("text/html")
            page = Page(response.text)
            assert (
                sum(
                    tag == "textarea" and attrs.get("id") == "kubeconfig-text"
                    for (tag, attrs), _ in page.elements
                )
                == 1
            )
            assert "secret()" not in "".join(page.scripts)
            text = "".join(page.textarea)
        assert yaml.safe_load(text) == expected_document(candidate_cluster)

        for path in ("/", "/nodes"):
            assert SECRET not in client.get(path).text
    assert_guest_read(export_agent, candidate_cluster)
    assert SECRET not in caplog.text
    assert inventory(conn) == before


def test_copy_target_is_outside_the_polling_grid(client, export_agent):
    page = Page(client.get("/").text)
    assert any(
        tag == "body" and attrs.get("hx-history") == "false"
        for (tag, attrs), _ in page.elements
    )
    forms = [
        attrs for (tag, attrs), _ in page.elements
        if tag == "form" and attrs.get("action") == "/kubeconfig/download"
    ]
    assert len(forms) == 1
    assert forms[0].get("method", "").lower() == "post"
    assert "hx-post" not in forms[0], "native form submission must handle the attachment"
    controls = [
        (attrs, ancestors)
        for (_, attrs), ancestors in page.elements
        if attrs.get("hx-post") == "/kubeconfig/copy"
    ]
    assert len(controls) == 1
    attrs, ancestors = controls[0]
    target = attrs.get("hx-target")
    assert target and target.startswith("#")
    panels = [ancestors for (_, attrs), ancestors in page.elements if attrs.get("id") == target[1:]]
    assert len(panels) == 1
    assert any(attrs.get("hx-get") == "/nodes" for (_, attrs), _ in page.elements)
    for chain in (ancestors, panels[0]):
        assert not any(attrs.get("hx-get") == "/nodes" for _, attrs in chain)
    poll = Page(client.get("/nodes").text)
    assert not any(attrs.get("id") == target[1:] for (_, attrs), _ in poll.elements)
    assert not export_agent.calls


@pytest.mark.parametrize("case", FAILURES)
@pytest.mark.parametrize("action,status", [("download", 503), ("copy", 200)])
def test_export_errors_are_readable_sanitized_and_not_cached(
    client, deployed_node, export_agent, conn, caplog, case, action, status
):
    message = script_failure(case, export_agent, deployed_node, conn)
    before = inventory(conn)
    with caplog.at_level("DEBUG"):
        headers = {"HX-Request": "true"} if action == "copy" else {}
        response = client.post(f"/kubeconfig/{action}", headers=headers)
        assert response.status_code == status
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-store"
        assert "content-disposition" not in response.headers
        page = Page(response.text)
        assert message in "".join(page.text)
        assert not page.textarea
        assert "secret()" not in "".join(page.scripts)
        close_buttons = [
            (attrs, ancestors) for (tag, attrs), ancestors in page.elements
            if tag == "button" and "data-dismiss-notification" in attrs
        ]
        assert len(close_buttons) == 1
        attrs, ancestors = close_buttons[0]
        assert attrs.get("type") == "button"
        assert attrs.get("aria-label") == "Dismiss notification"
        assert any("data-notification" in attrs for _, attrs in ancestors)
        assert not any("data-notification-id" in attrs for (_, attrs), _ in page.elements)
        scripts = [
            attrs for (tag, attrs), _ in page.elements
            if tag == "script" and attrs.get("src") == "/static/notifications.js"
        ]
        assert len(scripts) == (1 if action == "download" else 0)
        if action == "download":
            assert any(tag == "a" and attrs.get("href") == "/" for (tag, attrs), _ in page.elements)
        for path in ("/", "/nodes"):
            assert SECRET not in client.get(path).text
    assert SECRET not in response.text + caplog.text
    assert inventory(conn) == before
    if case == "missing-server":
        assert not export_agent.calls
    else:
        assert_guest_read(export_agent, deployed_node)
