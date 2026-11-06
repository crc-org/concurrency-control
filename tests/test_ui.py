"""The dashboard in a real browser (Playwright), served by web.app on a local
port and backed by the fake cluster.

Runs in the installed Google Chrome by default. For the other engines run
`python -m playwright install firefox webkit` once, then pass
--browser firefox --browser webkit. Page timers are frozen: tests move the
page clock forward to trigger the 10 s refresh or the 5 s log poll.
"""
import datetime
import json
import re
import threading

import pytest
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

import control
import machines
import pipeline_builder
import presets as presets_module
import task_templates
import web
from fake_ssh import private_key

pytestmark = pytest.mark.ui

START = datetime.datetime(2026, 11, 4, 10, 0, 0)
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


def pytest_generate_tests(metafunc):
    if "browser_name" in metafunc.fixturenames:
        metafunc.parametrize("browser_name", metafunc.config.getoption("browser") or ["chrome"], scope="session")


@pytest.fixture(scope="session")
def server_url():
    server = make_server("127.0.0.1", 0, web.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="session")
def browser(browser_name, pytestconfig):
    headless = not pytestconfig.getoption("headed")
    with sync_playwright() as playwright:
        if browser_name == "chrome":
            browser = playwright.chromium.launch(channel="chrome", headless=headless)
        else:
            browser = getattr(playwright, browser_name).launch(headless=headless)
        yield browser
        browser.close()


@pytest.fixture
def open_page(browser, server_url):
    """Opens the dashboard in a fresh browser context. Uncaught page errors fail the test."""
    contexts, errors = [], []

    def open_(token=None, **context_options):
        context = browser.new_context(base_url=server_url, **context_options)
        contexts.append(context)
        if token is not None:
            # as if this tab was unlocked earlier; the page trusts a stored token until a write fails
            context.add_init_script(f"try {{ sessionStorage.setItem('machineApiToken', {json.dumps(token)}) }} catch {{}}")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(error))
        page.clock.install(time=START)
        page.clock.pause_at(START)
        page.goto("/")
        return page

    yield open_
    for context in contexts:
        context.close()
    assert not errors, f"uncaught errors in the page: {errors}"


def rows(page, table):
    """The visible text of each body row, cell by cell."""
    return page.locator(f"#{table} tbody tr").evaluate_all(
        "rows => rows.map(row => [...row.cells].map(cell => cell.innerText.trim()))")


def machine_row(page, name):
    index = page.locator("#machines tbody tr").evaluate_all(
        "(rows, name) => rows.findIndex(row => row.cells[0].firstChild.textContent === name)", name)
    assert index >= 0, f"no row for machine {name}"
    return page.locator("#machines tbody tr").nth(index)


def remove_button(page, name):
    return machine_row(page, name).get_by_role("button", name="Remove")


def unlock(page, token):
    page.fill("#token-input", token)
    page.click("#unlock-form button")
    expect(page.locator("#unlocked")).to_be_visible()


def fill_machine(page, name="linux-arm64-1", host="10.0.0.5", user="crcqe", key=KEY):
    page.fill("#f-name", name)
    page.fill("#f-host", host)
    page.fill("#f-user", user)
    page.fill("#f-key", key)


def advance_to_refresh(page, ms=10_000):
    """Moves the page clock so the periodic refresh runs, and waits until it has rendered."""
    before = page.text_content("#updated")
    page.clock.run_for(ms)
    expect(page.locator("#updated")).not_to_have_text(before)


def poll_log(page):
    with page.expect_response("**/log"):
        page.clock.run_for(5_000)


def distance_from_log_end(page):
    return page.eval_on_selector("#log", "el => el.scrollHeight - el.scrollTop - el.clientHeight")


def wait_until(page, condition, timeout_ms=5_000):
    for _ in range(timeout_ms // 50):
        if condition():
            return
        page.wait_for_timeout(50)
    raise AssertionError("condition not met in time")


# ---------------- what everyone sees ----------------

def test_machines_are_listed_sorted_with_status_and_users(fake, open_page):
    fake.set_machine("win-1", "busy")
    fake.set_machine("linux-1", "free")
    fake.set_machine("mac-1", "maintenance")
    fake.set_holds(runs=[("run-a", "win-1")], tasks=[("run-b", "win-1")])
    page = open_page()
    assert rows(page, "machines") == [
        ["linux-1", "free", "—"],
        ["mac-1", "maintenance", "—"],
        ["win-1", "busy", "run-a, run-b"],
    ]
    expect(page.locator("#machines .badge")).to_have_class(["badge free", "badge other", "badge busy"])


def test_running_pipelines_say_what_releases_the_machine(fake, open_page):
    fake.set_holds(runs=[("run-a", "linux-1")], tasks=[("run-a", "win-1")])
    page = open_page()
    assert rows(page, "runs") == [
        ["run-a", "linux-1", "run finishes"],
        ["run-a", "win-1", "task build-installer finishes"],
    ]


def test_empty_tables(open_page):
    page = open_page()
    assert rows(page, "machines") == [["No machines registered yet."]]
    assert rows(page, "runs") == [["Nothing is running."]]


def test_server_data_is_shown_as_text(fake, log_file, open_page):
    evil = '<img src=x onerror="window.pwned = 1">'
    fake.set_machine("linux-1", evil)
    fake.set_holds(runs=[(evil, "linux-1")])
    log_file.write_text(evil + "\n")
    page = open_page()
    expect(page.locator("#machines .badge")).to_have_text(evil)
    expect(page.locator("#log")).to_contain_text(evil)
    advance_to_refresh(page)  # the same data again, this time through fetch
    poll_log(page)
    expect(page.locator("#runs td.name").first).to_contain_text(evil)
    assert page.locator("img").count() == 0
    assert page.evaluate("window.pwned") is None


def test_log_starts_scrolled_to_the_end(log_file, open_page):
    log_file.write_text("".join(f"line {i}\n" for i in range(100)))
    page = open_page()
    assert distance_from_log_end(page) < 2


@pytest.mark.parametrize("scheme,background", [("light", "rgb(245, 246, 248)"), ("dark", "rgb(19, 22, 27)")])
def test_follows_the_color_scheme(open_page, scheme, background):
    page = open_page(color_scheme=scheme)
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == background


@pytest.mark.parametrize("width", [320, 390])
def test_phone_layout(fake, api_token, open_page, width):
    long_name = "linux-arm64-" + "x" * 51  # 63 characters, the longest allowed
    fake.set_machine(long_name, "busy")
    fake.set_machine("mac-1", "free")
    fake.set_holds(runs=[("crc-installer-run-abcde", long_name)])
    page = open_page(token=api_token, viewport={"width": width, "height": 800})
    expect(page.locator("#machines th.col-extra")).to_be_hidden()
    expect(page.locator("#machines .sub")).to_be_visible()
    expect(page.locator("#machines .sub")).to_have_text("crc-installer-run-abcde")
    assert page.evaluate("document.documentElement.scrollWidth") <= width


# ---------------- read-only ----------------

def test_read_only_without_api_token(fake, open_page):
    fake.set_machine("linux-1", "free")
    page = open_page(token="left-over")
    expect(page.locator("#api-disabled")).to_be_visible()
    for hidden in ("#auth", "#add-card", "#action-col"):
        expect(page.locator(hidden)).to_be_hidden()
    expect(page.get_by_role("button", name="Remove")).to_have_count(0)


# ---------------- unlocking ----------------

def test_locked_until_the_token_is_entered(fake, api_token, open_page):
    fake.set_machine("linux-1", "free")
    page = open_page()
    expect(page.locator("#unlock-form")).to_be_visible()
    expect(page.locator("#locked-hint")).to_be_visible()
    for hidden in ("#api-disabled", "#unlocked", "#add-form", "#action-col"):
        expect(page.locator(hidden)).to_be_hidden()
    expect(page.get_by_role("button", name="Remove")).to_have_count(0)


def test_locked_hint_focuses_the_token_field(api_token, open_page):
    page = open_page()
    page.click("#locked-unlock")
    expect(page.locator("#token-input")).to_be_focused()


def test_unlock_needs_a_token(api_token, open_page):
    page = open_page()
    page.click("#unlock-form button")
    expect(page.locator("#token-msg")).to_have_text("Enter the admin token first.")
    expect(page.locator("#token-input")).to_be_focused()


def test_wrong_token_is_refused(api_token, open_page):
    page = open_page()
    page.fill("#token-input", "nope")
    page.click("#unlock-form button")
    expect(page.locator("#token-msg")).to_have_text("Wrong admin token.")
    expect(page.locator("#unlocked")).to_be_hidden()
    assert page.evaluate("sessionStorage.getItem('machineApiToken')") is None


def test_unlock_shows_the_admin_controls(fake, api_token, open_page):
    fake.set_machine("linux-1", "free")
    page = open_page()
    page.fill("#token-input", f"  {api_token}  ")
    page.press("#token-input", "Enter")
    expect(page.locator("#unlocked")).to_be_visible()
    expect(page.locator("#add-form")).to_be_visible()
    expect(page.locator("#token-input")).to_have_value("")
    expect(remove_button(page, "linux-1")).to_be_enabled()
    assert page.evaluate("sessionStorage.getItem('machineApiToken')") == api_token


def test_unlock_lasts_for_this_tab_only(api_token, open_page):
    page = open_page()
    unlock(page, api_token)
    page.reload()
    expect(page.locator("#unlocked")).to_be_visible()
    other_tab = page.context.new_page()
    other_tab.goto("/")
    expect(other_tab.locator("#unlock-form")).to_be_visible()


def test_lock_forgets_the_token(api_token, open_page):
    page = open_page()
    unlock(page, api_token)
    page.click("#lock-btn")
    expect(page.locator("#unlock-form")).to_be_visible()
    expect(page.locator("#add-form")).to_be_hidden()
    assert page.evaluate("sessionStorage.getItem('machineApiToken')") is None


def test_rejected_token_locks_the_page(fake, api_token, open_page):
    # e.g. API_TOKEN was rotated while the tab still had the old one
    page = open_page(token="old-token")
    expect(page.locator("#unlocked")).to_be_visible()
    fill_machine(page)
    page.click("#add-btn")
    expect(page.locator("#token-msg")).to_have_text("The admin token was rejected, unlock again.")
    expect(page.locator("#unlock-form")).to_be_visible()
    expect(page.locator("#add-form")).to_be_hidden()
    assert fake.machines() == {}


# ---------------- adding machines ----------------

def test_add_machine(fake, api_token, open_page):
    page = open_page(token=api_token)
    fill_machine(page)
    with page.expect_request(lambda request: request.method == "POST") as request:
        page.click("#add-btn")
    assert request.value.post_data_json == {
        "name": "linux-arm64-1", "host": "10.0.0.5", "username": "crcqe", "private_key": KEY}
    expect(page.locator("#add-msg")).to_have_text(
        "Added linux-arm64-1. Pipelines can use it once they name it in their machine label.")
    expect(page.locator("#add-msg")).to_have_class("msg ok")
    expect(page.locator("#machines td.name")).to_have_text(["linux-arm64-1"])
    assert rows(page, "machines") == [["linux-arm64-1", "free", "—", "Check\nRemove"]]
    for field in ("#f-name", "#f-host", "#f-user", "#f-key"):
        expect(page.locator(field)).to_have_value("")
    assert fake.secret("linux-arm64-1")["data"] == {"host": "10.0.0.5", "username": "crcqe", "id_rsa": KEY}


def test_name_field_accepts_what_the_api_accepts(api_token, open_page):
    names = ["a", "0", "linux-arm64-1", "a" * 63, "a" * 64, "", "Linux-1", "linux_1", "linux.1",
             "-linux", "linux-", "linux 1", "línux", "a--b"]
    page = open_page(token=api_token)
    in_browser = page.eval_on_selector(
        "#f-name", "(el, names) => names.map(name => { el.value = name; return el.checkValidity(); })", names)
    assert dict(zip(names, in_browser)) == {name: machines.valid_name(name) for name in names}


def test_invalid_name_is_stopped_before_sending(fake, api_token, open_page):
    page = open_page(token=api_token)
    posts = []
    page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
    fill_machine(page, name="Linux_1")
    page.click("#add-btn")
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#f-name", "el => el.validity.patternMismatch")
    assert posts == [] and fake.machines() == {}


def test_add_machine_shows_the_server_error(fake, api_token, open_page):
    fake.set_machine("linux-arm64-1", "busy")
    page = open_page(token=api_token)
    fill_machine(page)
    page.click("#add-btn")
    expect(page.locator("#add-msg")).to_have_text("machine linux-arm64-1 already exists")
    expect(page.locator("#add-msg")).to_have_class("msg error")
    expect(page.locator("#f-host")).to_have_value("10.0.0.5")
    expect(page.locator("#add-btn")).to_be_enabled()


def test_add_button_is_disabled_while_saving(fake, api_token, open_page):
    page = open_page(token=api_token)
    held = []
    page.route("**/api/machines",
               lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    fill_machine(page)
    page.click("#add-btn")
    expect(page.locator("#add-btn")).to_be_disabled()
    wait_until(page, lambda: held)
    page.press("#f-host", "Enter")  # submitting again from the keyboard mustn't send a second request
    page.wait_for_timeout(300)
    assert len(held) == 1
    held[0].continue_()
    expect(page.locator("#add-msg")).to_have_class("msg ok")
    expect(page.locator("#add-btn")).to_be_enabled()
    assert fake.machines() == {"linux-arm64-1": "free"}


def test_add_machine_network_failure(api_token, open_page):
    page = open_page(token=api_token)
    page.route("**/api/machines",
               lambda route: route.abort() if route.request.method == "POST" else route.continue_())
    fill_machine(page)
    page.click("#add-btn")
    expect(page.locator("#add-msg")).to_have_class("msg error")
    expect(page.locator("#add-msg")).not_to_be_empty()
    expect(page.locator("#add-btn")).to_be_enabled()


def test_add_machine_error_page_without_json(api_token, open_page):
    page = open_page(token=api_token)
    page.route("**/api/machines",
               lambda route: route.fulfill(status=502, content_type="text/html", body="<h1>Bad Gateway</h1>")
               if route.request.method == "POST" else route.continue_())
    fill_machine(page)
    page.click("#add-btn")
    expect(page.locator("#add-msg")).to_have_text(re.compile(r"^502\b"))
    expect(page.locator("h1", has_text="Bad Gateway")).to_have_count(0)


def test_key_can_be_loaded_from_a_file(api_token, open_page):
    page = open_page(token=api_token)
    page.set_input_files("#f-keyfile", files=[{"name": "id_rsa", "mimeType": "text/plain", "buffer": KEY.encode()}])
    expect(page.locator("#f-key")).to_have_value(KEY)
    assert page.eval_on_selector("#f-keyfile", "el => el.value") == ""


def test_page_does_not_keep_the_private_key(fake, api_token, open_page):
    page = open_page(token=api_token)
    fill_machine(page)
    page.click("#add-btn")
    expect(page.locator("#add-msg")).to_have_class("msg ok")
    expect(page.locator("#f-key")).to_have_value("")
    assert "PRIVATE KEY" not in page.evaluate("JSON.stringify([{...sessionStorage}, {...localStorage}])")


def test_success_message_fades_and_errors_stay(fake, api_token, open_page):
    page = open_page(token=api_token)
    fill_machine(page)
    page.click("#add-btn")
    expect(page.locator("#add-msg")).to_have_class("msg ok")
    page.clock.run_for(8_000)
    expect(page.locator("#add-msg")).to_be_hidden()
    fill_machine(page)  # the same name again
    page.click("#add-btn")
    expect(page.locator("#add-msg")).to_have_class("msg error")
    page.clock.run_for(30_000)
    expect(page.locator("#add-msg")).to_be_visible()


@pytest.mark.xfail(strict=True, reason="the file input is display:none, so 'Load from file…' can't be reached "
                                       "with the keyboard")
def test_key_file_can_be_chosen_with_the_keyboard(api_token, open_page, browser_name):
    if browser_name == "webkit":
        pytest.skip("WebKit, like Safari, only moves focus to text fields with Tab by default")
    page = open_page(token=api_token)
    page.focus("#f-key")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") != "add-btn"


# ---------------- removing machines ----------------

def test_remove_machine(fake, api_token, open_page):
    fake.set_machine("linux-1", "free")
    fake.set_machine("mac-1", "free")
    page = open_page(token=api_token)
    questions = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.accept()))
    remove_button(page, "linux-1").click()
    expect(page.locator("#machines-msg")).to_have_text("Removed linux-1.")
    expect(page.locator("#machines td.name")).to_have_text(["mac-1"])
    assert questions[0].startswith("Remove machine linux-1?")
    assert fake.machines() == {"mac-1": "free"}


def test_cancelled_remove_does_nothing(fake, api_token, open_page):
    fake.set_machine("linux-1", "free")
    page = open_page(token=api_token)
    deletes = []
    page.on("request", lambda request: deletes.append(request) if request.method == "DELETE" else None)
    page.on("dialog", lambda dialog: dialog.dismiss())
    remove_button(page, "linux-1").click()
    page.wait_for_timeout(300)
    assert deletes == [] and fake.machines() == {"linux-1": "free"}
    expect(remove_button(page, "linux-1")).to_be_enabled()


def test_busy_machine_cannot_be_removed(fake, api_token, open_page):
    fake.set_machine("linux-1", "busy")
    page = open_page(token=api_token)
    expect(remove_button(page, "linux-1")).to_be_disabled()
    expect(remove_button(page, "linux-1")).to_have_attribute(
        "title", "A busy machine can't be removed. Wait until its run finishes.")


def test_failed_remove_explains_and_allows_a_retry(fake, api_token, open_page):
    fake.set_machine("linux-1", "free")
    page = open_page(token=api_token)
    fake.set_machine("linux-1", "busy")  # a run took it after the page loaded
    page.on("dialog", lambda dialog: dialog.accept())
    remove_button(page, "linux-1").click()
    expect(page.locator("#machines-msg")).to_have_text("machine linux-1 is busy, retry when it is free")
    expect(page.locator("#machines-msg")).to_have_class("msg error")
    expect(remove_button(page, "linux-1")).to_be_enabled()
    assert fake.machines() == {"linux-1": "busy"}


@pytest.mark.xfail(strict=True, reason="Remove is only disabled for status 'busy', not while a run still holds "
                                       "the machine")
def test_machine_held_by_a_run_cannot_be_removed(fake, api_token, open_page):
    fake.set_machine("linux-1", "maintenance")  # drained, but run-a still uses it
    fake.set_holds(runs=[("run-a", "linux-1")])
    page = open_page(token=api_token)
    expect(remove_button(page, "linux-1")).to_be_disabled(timeout=1_000)


# ---------------- checking machines ----------------

def add_reachable_machine(fake, name="linux-1", status="free"):
    fake.set_machine(name, status)
    fake.add_secret(name, host="10.0.0.5", username="crcqe", id_rsa=private_key())


def check_button(page, name):
    return machine_row(page, name).get_by_role("button", name=re.compile("^Check"))


def check_items(page):
    """(icon, item, detail) of each line in the open check results."""
    return page.locator(".check-list li").evaluate_all(
        "items => items.map(li => [...li.children].map(span => span.textContent))")


def test_check_needs_unlocking(fake, api_token, open_page):
    add_reachable_machine(fake)
    page = open_page()
    expect(page.get_by_role("button", name="Check")).to_have_count(0)
    unlock(page, api_token)
    expect(check_button(page, "linux-1")).to_be_visible()


def test_check_lists_what_the_machine_has_and_what_is_missing(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake)
    del fake_ssh.answers["go version"]
    page = open_page(token=api_token)
    check_button(page, "linux-1").click()
    expect(page.locator(".check-head .problems")).to_have_text("1 problem")
    assert check_items(page) == [
        ["✓", "credentials", "crcqe@10.0.0.5"],
        ["✓", "SSH key", "ssh-ed25519"],
        ["✓", "SSH login", "connected"],
        ["✓", "Linux shell", "Linux"],
        ["✓", "git", "git version 2.45.1"],
        ["✓", "make", "GNU Make 4.4.1"],
        ["✗", "go", "not found"],
        ["✓", "passwordless sudo", "ok"],
    ]
    expect(page.locator(".check-list li.fail")).to_have_count(1)
    expect(page.locator(".check-head")).to_contain_text("checked ")


def test_check_of_a_healthy_machine(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake)
    page = open_page(token=api_token)
    check_button(page, "linux-1").click()
    expect(page.locator(".check-head .all-ok")).to_have_text("Everything is in place")
    expect(page.locator(".check-list li.fail")).to_have_count(0)


def test_check_results_sit_under_their_machine(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake, "linux-1")
    add_reachable_machine(fake, "mac-1")
    page = open_page(token=api_token)
    check_button(page, "linux-1").click()
    expect(page.locator(".check-row")).to_have_count(1)
    order = page.locator("#machines tbody tr").evaluate_all(
        "rows => rows.map(row => row.classList.contains('check-row') ? 'results' : row.cells[0].firstChild.textContent)")
    assert order == ["linux-1", "results", "mac-1"]


def test_check_button_waits_for_the_answer(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake)
    page = open_page(token=api_token)
    held = []
    page.route("**/check", lambda route: held.append(route))
    check_button(page, "linux-1").click()
    expect(check_button(page, "linux-1")).to_have_text("Checking…")
    expect(check_button(page, "linux-1")).to_be_disabled()
    expect(page.locator(".check-row")).to_have_text("Logging in to linux-1…")
    wait_until(page, lambda: held)
    advance_to_refresh(page)  # a refresh meanwhile keeps the waiting state
    expect(check_button(page, "linux-1")).to_have_text("Checking…")
    held[0].continue_()
    expect(page.locator(".check-head .all-ok")).to_be_visible()
    expect(check_button(page, "linux-1")).to_have_text("Check")
    expect(check_button(page, "linux-1")).to_be_enabled()


def test_check_works_on_a_busy_machine(fake, fake_ssh, api_token, open_page):
    # it only reads, so it doesn't disturb the run
    add_reachable_machine(fake, status="busy")
    page = open_page(token=api_token)
    expect(check_button(page, "linux-1")).to_be_enabled()
    check_button(page, "linux-1").click()
    expect(page.locator(".check-head .all-ok")).to_be_visible()


def test_check_results_stay_through_the_refresh(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake)
    page = open_page(token=api_token)
    check_button(page, "linux-1").click()
    expect(page.locator(".check-row")).to_be_visible()
    advance_to_refresh(page)
    expect(page.locator(".check-row")).to_be_visible()


def test_hide_closes_the_check_results(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake)
    page = open_page(token=api_token)
    check_button(page, "linux-1").click()
    page.locator(".check-head").get_by_role("button", name="Hide").click()
    expect(page.locator(".check-row")).to_have_count(0)


def test_lock_clears_the_check_results(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake)
    page = open_page(token=api_token)
    check_button(page, "linux-1").click()
    expect(page.locator(".check-row")).to_be_visible()
    page.click("#lock-btn")
    expect(page.locator(".check-row")).to_have_count(0)
    unlock(page, api_token)
    expect(page.locator(".check-row")).to_have_count(0)


def test_failed_check_request_is_explained(fake, api_token, open_page):
    add_reachable_machine(fake)
    page = open_page(token=api_token)
    page.route("**/check", lambda route: route.fulfill(status=500, json={"error": "boom"}))
    check_button(page, "linux-1").click()
    expect(page.locator("#machines-msg")).to_have_text("Couldn't check linux-1: boom")
    expect(page.locator("#machines-msg")).to_have_class("msg error")
    expect(page.locator(".check-row")).to_have_count(0)
    expect(check_button(page, "linux-1")).to_be_enabled()


def test_check_details_are_shown_as_text(fake, fake_ssh, api_token, open_page):
    add_reachable_machine(fake)
    fake_ssh.answers["git --version"] = (0, "<img src=x onerror=alert(1)>\n", "")
    page = open_page(token=api_token)
    check_button(page, "linux-1").click()
    expect(page.locator(".check-list")).to_contain_text("<img src=x onerror=alert(1)>")
    expect(page.locator(".check-list img")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390])
def test_check_results_on_a_phone(fake, fake_ssh, api_token, open_page, width):
    add_reachable_machine(fake, "linux-arm64-1")
    fake_ssh.answers["sudo -n true && echo ok"] = (1, "", "sudo: a password is required\n")
    page = open_page(token=api_token, viewport={"width": width, "height": 800})
    check_button(page, "linux-arm64-1").click()
    expect(page.locator(".check-head .problems")).to_have_text("1 problem")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert page.eval_on_selector("#machines", "t => t.closest('.table-wrap').scrollWidth <= t.closest('.table-wrap').clientWidth")


# ---------------- pipeline presets ----------------

PRESET_CM = "pipeline-presets"


def add_pipeline_setup(fake):
    fake.set_machine("linux-1", "free")
    fake.set_machine("windows-11", "busy")
    fake.add_pipeline("crc-e2e", description="CRC end-to-end tests",
                      params=[("crc-version", None), ("bundle", "4.17"), ("tags", ["e2e"], "array")],
                      workspaces=["source", ("cache", True)])


def store_preset(fake, name, **fields):
    preset = {"pipeline": "crc-e2e", "machine": "linux-1", "task_machine": "", "params": {"crc-version": "2.45.0"},
              "workspaces": {"source": {"type": "volumeClaimTemplate", "value": "1Gi"}},
              "service_account": "", "timeout": ""}
    preset.update(fields)
    if PRESET_CM not in fake.objects["configmap"]:
        fake.add("configmap", PRESET_CM, data={})
    fake.data(PRESET_CM)[name] = json.dumps(preset)


def stored_presets(fake):
    if PRESET_CM not in fake.objects["configmap"]:
        return {}
    return {k: json.loads(v) for k, v in fake.data(PRESET_CM).items()}


def preset_button(page, name, label):
    index = page.locator("#presets tbody tr").evaluate_all(
        "(rows, name) => rows.findIndex(row => row.cells[0].firstChild.textContent === name)", name)
    assert index >= 0, f"no row for preset {name}"
    return page.locator("#presets tbody tr").nth(index).get_by_role("button", name=re.compile(f"^{label}"))


def open_new_preset(page):
    page.click("#new-preset-btn")
    expect(page.locator("#p-pipeline option")).to_have_text(["crc-e2e"])


def test_presets_are_listed_for_everyone(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "e2e-linux")
    store_preset(fake, "e2e-both", task_machine="windows-11")
    page = open_page()
    assert rows(page, "presets") == [["e2e-both", "crc-e2e", "linux-1 + windows-11"], ["e2e-linux", "crc-e2e", "linux-1"]]
    expect(page.locator("#presets button")).to_have_count(0)
    expect(page.locator("#new-preset-btn")).to_be_hidden()
    expect(page.locator("#presets-locked-hint")).to_be_visible()


def test_no_presets_yet(fake, api_token, open_page):
    page = open_page(token=api_token)
    assert rows(page, "presets") == [["No presets yet."]]


def test_presets_are_off_without_preset_cm(fake, monkeypatch, open_page):
    monkeypatch.setattr(presets_module, "PRESET_CM", None)
    page = open_page()
    expect(page.locator("#presets-disabled")).to_be_visible()
    expect(page.locator("#presets-wrap")).to_be_hidden()


def test_create_preset_from_the_pipeline_fields(fake, api_token, open_page):
    add_pipeline_setup(fake)
    page = open_page(token=api_token)
    open_new_preset(page)
    expect(page.locator("#p-pipeline-desc")).to_have_text("CRC end-to-end tests")
    # parameters come from the pipeline, with its defaults
    expect(page.locator("#p-params .label")).to_have_text(["crc-version required", "bundle", "tags"])
    expect(page.locator('[data-param="crc-version"]')).to_have_attribute("placeholder", "required")
    expect(page.locator('[data-param="bundle"]')).to_have_attribute("placeholder", "default: 4.17")
    expect(page.locator('[data-param="tags"]')).to_be_disabled()
    # a required workspace starts as a new volume, an optional one unbound
    expect(page.locator('[data-workspace="source"] select')).to_have_value("volumeClaimTemplate")
    expect(page.locator('[data-workspace="source"] input')).to_have_attribute("placeholder", "size, e.g. 1Gi")
    expect(page.locator('[data-workspace="cache"] select')).to_have_value("")
    expect(page.locator('[data-workspace="cache"] input')).to_be_hidden()
    expect(page.locator("#p-machine option")).to_have_text(["linux-1 (free)", "windows-11 (busy)"])
    expect(page.locator("#p-task-machine option")).to_have_text(["None", "linux-1 (free)", "windows-11 (busy)"])

    page.fill("#p-name", "e2e-linux")
    page.fill('[data-param="crc-version"]', "2.45.0")
    page.fill('[data-workspace="source"] input', "1Gi")
    page.click("#preset-save")
    expect(page.locator("#presets-msg")).to_have_text("Saved preset e2e-linux.")
    expect(page.locator("#preset-form")).to_be_hidden()
    assert rows(page, "presets")[0][:3] == ["e2e-linux", "crc-e2e", "linux-1"]
    assert stored_presets(fake) == {"e2e-linux": {
        "pipeline": "crc-e2e", "machine": "linux-1", "task_machine": "", "params": {"crc-version": "2.45.0"},
        "workspaces": {"source": {"type": "volumeClaimTemplate", "value": "1Gi"}},
        "service_account": "", "timeout": ""}}


def test_missing_required_value_is_stopped_before_sending(fake, api_token, open_page):
    add_pipeline_setup(fake)
    page = open_page(token=api_token)
    open_new_preset(page)
    page.fill("#p-name", "e2e-linux")
    page.fill('[data-workspace="source"] input', "1Gi")
    sent = []
    page.on("request", lambda request: sent.append(request) if request.method == "PUT" else None)
    page.click("#preset-save")
    page.wait_for_timeout(300)
    assert sent == [] and stored_presets(fake) == {}
    assert not page.eval_on_selector("#preset-form", "form => form.checkValidity()")


def test_server_refusal_is_shown_in_the_editor(fake, api_token, open_page):
    add_pipeline_setup(fake)
    page = open_page(token=api_token)
    open_new_preset(page)
    page.fill("#p-name", "e2e-linux")
    page.fill('[data-param="crc-version"]', "2.45.0")
    page.select_option('[data-workspace="source"] select', "secret")
    page.fill('[data-workspace="source"] input', "Not_A_Name")
    page.click("#preset-save")
    expect(page.locator("#preset-msg")).to_have_text("workspace source: 'Not_A_Name' isn't a valid object name")
    expect(page.locator("#preset-msg")).to_have_class("msg error")
    expect(page.locator("#preset-form")).to_be_visible()


def test_edit_preset_starts_from_its_values(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "e2e-linux", params={"crc-version": "2.45.0", "bundle": "4.18"},
                 workspaces={"source": {"type": "persistentVolumeClaim", "value": "crc-src"}, "cache": {"type": "emptyDir", "value": ""}},
                 task_machine="windows-11", service_account="pipeline", timeout="3h")
    page = open_page(token=api_token)
    preset_button(page, "e2e-linux", "Edit").click()
    expect(page.locator("#preset-form-title")).to_have_text("Edit preset e2e-linux")
    expect(page.locator("#p-name")).to_have_value("e2e-linux")
    expect(page.locator("#p-name")).to_have_attribute("readonly", "")
    expect(page.locator('[data-param="bundle"]')).to_have_value("4.18")
    expect(page.locator('[data-workspace="source"] select')).to_have_value("persistentVolumeClaim")
    expect(page.locator('[data-workspace="source"] input')).to_have_value("crc-src")
    expect(page.locator('[data-workspace="cache"] select')).to_have_value("emptyDir")
    expect(page.locator("#p-task-machine")).to_have_value("windows-11")
    expect(page.locator("#p-advanced")).to_have_attribute("open", "")
    expect(page.locator("#p-sa")).to_have_value("pipeline")
    page.fill('[data-param="crc-version"]', "2.46.0")
    page.click("#preset-save")
    expect(page.locator("#presets-msg")).to_have_text("Saved preset e2e-linux.")
    assert stored_presets(fake)["e2e-linux"]["params"] == {"crc-version": "2.46.0", "bundle": "4.18"}
    assert stored_presets(fake)["e2e-linux"]["timeout"] == "3h"


def test_editing_a_preset_whose_pipeline_is_gone(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "old", pipeline="crc-old")
    page = open_page(token=api_token)
    preset_button(page, "old", "Edit").click()
    expect(page.locator("#p-pipeline option")).to_have_text(["crc-old (no longer exists)", "crc-e2e"])
    page.select_option("#p-pipeline", "crc-e2e")
    expect(page.locator('[data-param="crc-version"]')).to_be_visible()


def test_switching_the_pipeline_switches_its_fields(fake, api_token, open_page):
    add_pipeline_setup(fake)
    fake.add_pipeline("crc-installer", params=[("installer-version", None)])
    page = open_page(token=api_token)
    page.click("#new-preset-btn")
    expect(page.locator("#p-pipeline option")).to_have_text(["crc-e2e", "crc-installer"])
    page.select_option("#p-pipeline", "crc-installer")
    expect(page.locator("#p-params .label")).to_have_text(["installer-version required"])
    expect(page.locator("#p-ws-set")).to_be_hidden()


def test_new_preset_with_a_taken_name_asks_first(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "e2e-linux", params={"crc-version": "1.0.0"})
    page = open_page(token=api_token)
    open_new_preset(page)
    page.fill("#p-name", "e2e-linux")
    page.fill('[data-param="crc-version"]', "2.45.0")
    page.fill('[data-workspace="source"] input', "1Gi")
    questions = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.dismiss()))
    page.click("#preset-save")
    wait_until(page, lambda: questions)
    assert questions == ["Preset e2e-linux already exists. Replace it?"]
    assert stored_presets(fake)["e2e-linux"]["params"] == {"crc-version": "1.0.0"}


def test_start_preset(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "e2e-linux")
    page = open_page(token=api_token)
    held = []
    page.route("**/start", lambda route: held.append(route))
    preset_button(page, "e2e-linux", "Start").click()
    expect(preset_button(page, "e2e-linux", "Start")).to_have_text("Starting…")
    expect(preset_button(page, "e2e-linux", "Start")).to_be_disabled()
    wait_until(page, lambda: held)
    held[0].continue_()
    expect(page.locator("#presets-msg")).to_have_text(re.compile(
        r"^Started e2e-linux-[a-z0-9]{5}\. It runs as soon as linux-1 is free\.$"))
    [run] = fake.objects["pipelinerun"].values()
    assert run["metadata"]["labels"]["concurrency-control/preset"] == "e2e-linux"
    assert run["spec"]["status"] == "PipelineRunPending"
    expect(preset_button(page, "e2e-linux", "Start")).to_have_text("Start")


def test_start_with_a_task_machine_names_both(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "both", task_machine="windows-11")
    page = open_page(token=api_token)
    preset_button(page, "both", "Start").click()
    expect(page.locator("#presets-msg")).to_contain_text("It runs as soon as linux-1 and windows-11 are free.")


def test_start_that_no_longer_fits_is_explained(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "e2e-linux", machine="mac-9")
    page = open_page(token=api_token)
    preset_button(page, "e2e-linux", "Start").click()
    expect(page.locator("#presets-msg")).to_have_text(
        "Couldn't start e2e-linux: preset e2e-linux no longer fits: machine mac-9 isn't registered, edit it first")
    expect(page.locator("#presets-msg")).to_have_class("msg error")
    assert fake.objects["pipelinerun"] == {}


def test_delete_preset(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "a")
    store_preset(fake, "b")
    page = open_page(token=api_token)
    questions = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.accept()))
    preset_button(page, "a", "Delete").click()
    expect(page.locator("#presets-msg")).to_have_text("Deleted preset a.")
    assert [r[0] for r in rows(page, "presets")] == ["b"]
    assert questions[0].startswith("Delete preset a?")
    assert list(stored_presets(fake)) == ["b"]


def test_cancelled_delete_keeps_the_preset(fake, api_token, open_page):
    add_pipeline_setup(fake)
    store_preset(fake, "a")
    page = open_page(token=api_token)
    page.on("dialog", lambda dialog: dialog.dismiss())
    preset_button(page, "a", "Delete").click()
    page.wait_for_timeout(300)
    assert list(stored_presets(fake)) == ["a"]


def test_cancel_and_lock_close_the_editor(fake, api_token, open_page):
    add_pipeline_setup(fake)
    page = open_page(token=api_token)
    open_new_preset(page)
    page.click("#preset-cancel")
    expect(page.locator("#preset-form")).to_be_hidden()
    open_new_preset(page)
    page.click("#lock-btn")
    expect(page.locator("#preset-form")).to_be_hidden()
    expect(page.locator("#new-preset-btn")).to_be_hidden()


def test_unreadable_pipelines_are_explained_in_the_editor(fake, api_token, open_page):
    add_pipeline_setup(fake)
    fake.fail("get", "pipeline", stderr="Error from server (Forbidden)")
    page = open_page(token=api_token)
    page.click("#new-preset-btn")
    expect(page.locator("#preset-msg")).to_have_text("Couldn't load the pipelines: can't read the pipelines from the cluster")
    expect(page.locator("#p-pipeline option")).to_have_text(["No pipelines in this namespace"])


def test_presets_refresh_every_10_seconds(fake, api_token, open_page):
    add_pipeline_setup(fake)
    page = open_page(token=api_token)
    store_preset(fake, "added-elsewhere")
    advance_to_refresh(page)
    assert rows(page, "presets")[0][0] == "added-elsewhere"


@pytest.mark.parametrize("width", [320, 390])
def test_presets_on_a_phone(fake, api_token, open_page, width):
    add_pipeline_setup(fake)
    store_preset(fake, "e2e-linux-arm64", task_machine="windows-11")
    page = open_page(token=api_token, viewport={"width": width, "height": 800})
    expect(page.locator("#presets .sub")).to_have_text("crc-e2e on linux-1 + windows-11")
    open_new_preset(page)
    page.select_option('[data-workspace="cache"] select', "persistentVolumeClaim")
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    assert page.eval_on_selector("#presets", "t => t.closest('.table-wrap').scrollWidth <= t.closest('.table-wrap').clientWidth")


# ---------------- building pipelines from templates ----------------

CHECK = "crc-support/tkn/check-bundle-url.yaml"
BUILD = "crc-builder/tkn/crc-builder.yaml"
E2E = "crc-e2e/task.yaml"


def step_card(page, index):
    return page.locator("#pl-steps .step-card").nth(index)


def choose_template(page, index, path):
    step_card(page, index).locator(".step-head select").first.select_option(path)


def input_row(page, index, param):
    return step_card(page, index).locator(f'.input-row[data-param="{param}"]')


def input_source(page, index, param):
    return input_row(page, index, param).locator("select").first


def built_pipeline(name="qe-quick"):
    return {"description": "quick QE", "steps": [
        {"name": "check", "template": CHECK, "inputs": {"bundle-base-url": {"from": "start"}, "bundle-name": {"from": "start"}}},
        {"name": "build", "template": BUILD},
        {"name": "test", "template": E2E, "run_after": ["check", "build"],
         "inputs": {"secret-host": {"from": "start"}, "os": {"from": "fixed", "value": "linux"},
                    "bundle-location": {"from": "step", "step": "check", "result": "bundle-base-url"}}},
    ]}


def test_pipelines_are_listed_with_where_they_were_made(fake, api_token, open_page):
    fake.add_pipeline("crc-qe-latest")
    pipeline_builder.save_pipeline("qe-quick", built_pipeline())
    page = open_page()
    expect(page.locator("#pipelines tbody tr")).to_have_count(2)
    assert rows(page, "pipelines") == [["crc-qe-latest", "1", "elsewhere"], ["qe-quick", "3", "here"]]
    unlock(page, api_token)
    expect(page.locator("#pipelines tbody tr").nth(0)).to_contain_text("read-only")
    expect(page.locator("#pipelines tbody tr").nth(1).get_by_role("button", name="Edit")).to_be_visible()


def test_build_a_pipeline_with_parallel_steps(fake, api_token, open_page):
    page = open_page(token=api_token)
    page.click("#new-pipeline-btn")
    expect(step_card(page, 0)).to_be_visible()
    choose_template(page, 0, CHECK)
    expect(step_card(page, 0).locator(".step-head input")).to_have_value("check-bundle-url")  # named after the template
    expect(input_source(page, 0, "bundle-base-url")).to_have_value("start")      # required: asked when starting
    expect(input_source(page, 0, "bundle-shasumfile")).to_have_value("default")  # has a default: uses it

    page.click("#pl-add-step")
    choose_template(page, 1, BUILD)
    expect(step_card(page, 1).locator(".waits input")).to_be_checked()  # waits for the previous step by default
    step_card(page, 1).locator(".waits input").uncheck()                # ...unless it should run in parallel

    page.click("#pl-add-step")
    choose_template(page, 2, E2E)
    step_card(page, 2).locator(".waits label").filter(has_text="check-bundle-url").locator("input").check()
    input_source(page, 2, "os").select_option("fixed")
    input_row(page, 2, "os").locator("input").fill("linux")
    input_source(page, 2, "bundle-location").select_option("step")
    expect(page.locator("#pl-order")).to_have_text("Order: check-bundle-url + crc-builder  →  crc-e2e-test")

    page.fill("#pl-name", "qe-quick")
    page.click("#pipeline-save")
    expect(page.locator("#pipelines-msg")).to_have_text("Saved pipeline qe-quick. Presets can use it now.")
    expect(page.locator("#pipeline-form")).to_be_hidden()
    tasks = fake.objects["pipeline"]["qe-quick"]["spec"]["tasks"]
    assert [(t["name"], t.get("runAfter")) for t in tasks] == [
        ("check-bundle-url", None), ("crc-builder", None), ("crc-e2e-test", ["crc-builder", "check-bundle-url"])]
    assert {p["name"]: p["value"] for p in tasks[2]["params"]} == {
        "secret-host": "$(params.secret-host)", "os": "linux",
        "bundle-location": "$(tasks.check-bundle-url.results.bundle-base-url)"}
    expect(page.locator("#pipelines tbody tr")).to_have_count(1)


def test_step_at_the_end_moves_last_and_waits_for_everything(fake, api_token, open_page):
    page = open_page(token=api_token)
    page.click("#new-pipeline-btn")
    choose_template(page, 0, BUILD)
    page.click("#pl-add-step")
    choose_template(page, 1, CHECK)
    step_card(page, 0).locator(".step-head select").nth(1).select_option("end")
    expect(step_card(page, 1)).to_have_class(re.compile("at-end"))
    expect(step_card(page, 1).locator(".step-head input")).to_have_value("crc-builder")
    expect(step_card(page, 1).locator(".waits")).to_have_count(0)
    expect(step_card(page, 0).locator(".waits")).to_contain_text("nothing, starts right away")
    expect(page.locator("#pl-order")).to_have_text("Order: check-bundle-url  →  at the end: crc-builder")


def test_removing_a_step_drops_what_pointed_at_it(fake, api_token, open_page):
    page = open_page(token=api_token)
    page.click("#new-pipeline-btn")
    choose_template(page, 0, CHECK)
    page.click("#pl-add-step")
    choose_template(page, 1, E2E)
    input_source(page, 1, "bundle-location").select_option("step")
    step_card(page, 0).get_by_role("button", name="Remove step").click()
    expect(page.locator("#pl-steps .step-card")).to_have_count(1)
    expect(step_card(page, 0).locator(".waits")).to_contain_text("nothing, starts right away")
    expect(input_source(page, 0, "bundle-location")).to_have_value("start")
    expect(input_source(page, 0, "bundle-location").locator("option")).not_to_contain_text(["Output of an earlier step"])


def test_edit_pipeline_starts_from_its_steps(fake, api_token, open_page):
    pipeline_builder.save_pipeline("qe-quick", built_pipeline())
    page = open_page(token=api_token)
    page.locator("#pipelines tbody tr").get_by_role("button", name="Edit").click()
    expect(page.locator("#pipeline-form-title")).to_have_text("Edit pipeline qe-quick")
    expect(page.locator("#pl-name")).to_have_attribute("readonly", "")
    expect(page.locator("#pl-desc")).to_have_value("quick QE")
    expect(page.locator("#pl-steps .step-card")).to_have_count(3)
    expect(input_source(page, 2, "bundle-location")).to_have_value("step")
    expect(input_row(page, 2, "os").locator("input")).to_have_value("linux")
    page.fill("#pl-desc", "quicker QE")
    page.click("#pipeline-save")
    expect(page.locator("#pipelines-msg")).to_have_text("Saved pipeline qe-quick. Presets can use it now.")
    assert fake.objects["pipeline"]["qe-quick"]["spec"]["description"] == "quicker QE"


def test_saving_over_a_pipeline_made_elsewhere_is_refused(fake, api_token, open_page):
    fake.add_pipeline("crc-qe-latest")
    page = open_page(token=api_token)
    page.click("#new-pipeline-btn")
    choose_template(page, 0, BUILD)
    page.fill("#pl-name", "crc-qe-latest")
    page.click("#pipeline-save")
    expect(page.locator("#pipeline-msg")).to_have_text(
        "pipeline crc-qe-latest already exists and wasn't made here, pick another name")
    expect(page.locator("#pipeline-form")).to_be_visible()


def test_delete_pipeline(fake, api_token, open_page):
    pipeline_builder.save_pipeline("qe-quick", built_pipeline())
    page = open_page(token=api_token)
    questions = []
    page.on("dialog", lambda dialog: (questions.append(dialog.message), dialog.accept()))
    page.locator("#pipelines tbody tr").get_by_role("button", name="Delete").click()
    expect(page.locator("#pipelines-msg")).to_have_text("Deleted pipeline qe-quick.")
    assert questions[0].startswith("Delete pipeline qe-quick?")
    assert "qe-quick" not in fake.objects["pipeline"]


def test_builder_without_templates_explains(fake, api_token, open_page, monkeypatch, tmp_path):
    monkeypatch.setattr(task_templates, "TEMPLATE_DIR", str(tmp_path))
    page = open_page(token=api_token)
    page.click("#new-pipeline-btn")
    expect(page.locator("#pipeline-msg")).to_have_text(f"Couldn't open the builder: no Task templates found in {tmp_path}")


def test_unreadable_pipelines_do_not_mark_the_page_stale(fake, open_page):
    fake.fail("get", "pipeline", stderr="Error from server (Forbidden)")
    page = open_page()
    expect(page.locator("#pipelines tbody")).to_have_text("Can't read the pipelines: can't read the pipelines from the cluster")
    advance_to_refresh(page)
    expect(page.locator("#updated")).not_to_have_class(re.compile("stale"))


@pytest.mark.parametrize("width", [320, 390])
def test_builder_on_a_phone(fake, api_token, open_page, width):
    page = open_page(token=api_token, viewport={"width": width, "height": 800})
    page.click("#new-pipeline-btn")
    choose_template(page, 0, CHECK)
    page.click("#pl-add-step")
    choose_template(page, 1, E2E)
    assert page.evaluate("document.documentElement.scrollWidth") <= width


# ---------------- staying current ----------------

def test_tables_refresh_every_10_seconds(fake, open_page):
    fake.set_machine("linux-1", "free")
    page = open_page()
    fake.set_machine("linux-1", "busy")
    fake.set_machine("mac-1", "free")
    fake.set_holds(runs=[("run-a", "linux-1")])
    page.clock.run_for(9_000)
    assert rows(page, "machines") == [["linux-1", "free", "—"]]
    advance_to_refresh(page, 1_000)
    assert rows(page, "machines") == [["linux-1", "busy", "run-a"], ["mac-1", "free", "—"]]
    assert rows(page, "runs") == [["run-a", "linux-1", "run finishes"]]


def test_unreachable_controller_is_flagged(open_page):
    page = open_page()
    page.route("**/api/machines", lambda route: route.abort())
    page.clock.run_for(10_000)
    expect(page.locator("#updated")).to_have_text("Can't reach the controller")
    expect(page.locator("#updated")).to_have_class(re.compile(r"\bstale\b"))
    page.unroute("**/api/machines")
    page.clock.run_for(10_000)
    expect(page.locator("#updated")).to_have_text(re.compile(r"^Updated "))
    expect(page.locator("#updated")).not_to_have_class(re.compile(r"\bstale\b"))


def test_dashboard_follows_a_run_scheduled_by_the_controller(fake, motd, open_page):
    fake.set_machine("linux-1", "free")
    fake.add_pipelinerun("run-a", labels={"machine": "linux-1"})
    page = open_page()
    control.monitor_pending_run()
    advance_to_refresh(page)
    assert rows(page, "machines") == [["linux-1", "busy", "run-a"]]
    assert rows(page, "runs") == [["run-a", "linux-1", "run finishes"]]
    fake.finish_pipelinerun("run-a")
    control.monitor_runs_finish()
    advance_to_refresh(page)
    assert rows(page, "machines") == [["linux-1", "free", "—"]]
    assert rows(page, "runs") == [["Nothing is running."]]


def test_log_follows_new_lines_at_the_end(log_file, open_page):
    log_file.write_text("".join(f"line {i}\n" for i in range(100)))
    page = open_page()
    with log_file.open("a") as f:
        f.write("".join(f"new {i}\n" for i in range(20)))
    poll_log(page)
    expect(page.locator("#log")).to_contain_text("new 19")
    assert distance_from_log_end(page) < 2


def test_log_stays_at_the_top_while_reading(log_file, open_page):
    log_file.write_text("".join(f"line {i}\n" for i in range(100)))
    page = open_page()
    page.eval_on_selector("#log", "el => { el.scrollTop = 0; }")
    with log_file.open("a") as f:
        f.write("".join(f"new {i}\n" for i in range(20)))
    poll_log(page)
    expect(page.locator("#log")).to_contain_text("new 19")
    assert page.eval_on_selector("#log", "el => el.scrollTop") == 0


@pytest.mark.xfail(strict=True, reason="the poll keeps scrollTop, but the log is a sliding window of the last 100 "
                                       "lines, so the text being read moves up as lines arrive")
def test_log_poll_does_not_move_the_text_being_read(log_file, open_page):
    log_file.write_text("".join(f"line {i}\n" for i in range(100)))
    page = open_page()
    top_line = ("el => el.textContent.split('\\n')"
                "[Math.round((el.scrollTop - 10) / parseFloat(getComputedStyle(el).lineHeight))]")
    page.eval_on_selector("#log", "el => { el.scrollTop = 10 + 30 * parseFloat(getComputedStyle(el).lineHeight); }")
    before = page.eval_on_selector("#log", top_line)
    with log_file.open("a") as f:
        f.write("".join(f"new {i}\n" for i in range(20)))
    poll_log(page)
    page.wait_for_timeout(300)
    assert page.eval_on_selector("#log", top_line) == before


@pytest.mark.xfail(strict=True, reason="the 5 s poll rewrites the log even when nothing changed, which clears a "
                                       "text selection")
def test_log_poll_keeps_a_text_selection(log_file, open_page):
    log_file.write_text("error: something broke\n")
    page = open_page()
    page.eval_on_selector("#log", "el => getSelection().selectAllChildren(el)")
    poll_log(page)
    page.wait_for_timeout(300)
    assert "something broke" in page.evaluate("getSelection().toString()")


@pytest.mark.xfail(strict=True, reason="the 10 s refresh rebuilds the table, so a focused Remove button is replaced "
                                       "and keyboard focus drops to the page")
def test_refresh_keeps_keyboard_focus(fake, api_token, open_page):
    fake.set_machine("linux-1", "free")
    fake.set_machine("mac-1", "free")
    page = open_page(token=api_token)
    remove_button(page, "mac-1").focus()
    advance_to_refresh(page)
    assert page.evaluate("document.activeElement.title") == "Remove mac-1"
