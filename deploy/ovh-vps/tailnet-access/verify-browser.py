from pathlib import Path

from playwright.sync_api import sync_playwright

site = dict(
    line.split("=", 1)
    for line in (Path(__file__).resolve().parent / "site.env").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)
host = "https://" + site["TAILNET_HOST"]

with sync_playwright() as p:
    b = p.chromium.launch()
    for title in ["SpiderFoot (OVH)"]:
        ctx = b.new_context()
        page = ctx.new_page()
        page.goto(site["DASHY_URL"], wait_until="networkidle")
        with page.expect_popup() as popup:
            page.locator("a.item").filter(has_text=title).click()
        app = popup.value
        app.wait_for_load_state("networkidle")
        if title == "Harvest":
            app.get_by_role("heading", name="Start an investigation", exact=True).wait_for()
        else:
            app.get_by_role("heading", name="Dashboard", exact=True).wait_for()
        assert app.url.startswith(host)
        assert app.locator("input[type=password]:visible").count() == 0
        print(title, "fresh Dashy click PASS", flush=True)
        ctx.close()
    for path in ["/", "/login", "/scans"]:
        ctx = b.new_context()
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e, captured=errors: captured.append(str(e)))
        page.goto(host + ":8443" + path, wait_until="networkidle")
        page.get_by_role("button", name="Sign out", exact=True).wait_for()
        assert "/login" not in page.url
        assert page.locator("input[type=password]:visible").count() == 0
        assert page.evaluate("localStorage.getItem('sf_access_token')") == "tailnet-proxy"
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.get_by_role("button", name="Sign out", exact=True).wait_for()
        assert not errors, errors
        print("SpiderFoot", path, "fresh and cleared storage PASS", flush=True)
        ctx.close()
    b.close()
