"""End-to-end tests against real native commands, in a throwaway directory.

Fixtures are built with CLI commands too. FILEMANAGER_CLI_PATH selects which
tool flavour is under test, so scripts/test.sh runs this for BSD and GNU.
"""
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from app import create_app  # noqa: E402
from cli import CLI, CommandError  # noqa: E402
from database import execute  # noqa: E402
from files import FileError, Storage, parse_ls, validate_name  # noqa: E402

CLI_PATH = os.environ.get("FILEMANAGER_CLI_PATH", config.DEFAULT_CLI_PATH)
PASSWORD = "correct horse"


def sh(*argv):
    return subprocess.run(argv, check=True, capture_output=True, env={"PATH": CLI_PATH})


class Base(unittest.TestCase):
    read_only = False

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="barefile-test-"))
        self.addCleanup(sh, "rm", "-rf", self.base)
        self.root = f"{self.base}/storage"
        self.cfg = config.Config(self.root, f"{self.base}/app", "test-secret", cli_path=CLI_PATH,
                                 read_only=self.read_only)
        self.app = create_app(self.cfg)
        self.client = self.app.test_client()

    def token(self, client=None):
        html = (client or self.client).get("/login", follow_redirects=True).get_data(as_text=True)
        match = re.search(r'name="csrf" content="(\w+)"', html)
        return match.group(1)

    def signup(self):
        token = self.token(self.client)
        res = self.client.post("/register", data={"username": "ahmad", "password": PASSWORD, "csrf_token": token})
        self.assertEqual(res.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.csrf = re.search(r'name="csrf" content="(\w+)"', page).group(1)

    def post(self, url, **kwargs):
        return self.client.post(url, headers={"X-CSRF-Token": self.csrf}, **kwargs)

    def op(self, action, items=None, **body):
        """Run an operation; return (lines, final event)."""
        res = self.post("/api/op", json={"action": action, "items": items or [], **body})
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        events = [json.loads(chunk[6:]) for chunk in res.get_data(as_text=True).split("\n\n") if chunk]
        return [e["line"] for e in events if "line" in e], events[-1]

    def upload(self, name, body=b"data", path=""):
        return self.post("/api/upload", query_string={"path": path, "name": name}, data=body,
                         content_type="application/octet-stream")

    def names(self, **query):
        html = self.client.get("/", query_string=query).get_data(as_text=True)
        return re.findall(r'class="select" value="([^"]*)"', html)

    def exists(self, rel):
        return os.path.lexists(f"{self.root}/{rel}")

    def read(self, rel):
        return sh("cat", f"{self.root}/{rel}").stdout

    def write(self, rel, body=b""):
        subprocess.run(["dd", f"of={self.root}/{rel}"], input=body, check=True, capture_output=True,
                       env={"PATH": CLI_PATH})

    def tmp_files(self):
        return sh("find", self.cfg.tmp, "-mindepth", "1").stdout


class AuthTests(Base):
    def test_first_account_only(self):
        self.signup()
        client = self.app.test_client()
        self.assertEqual(client.get("/register").status_code, 302)
        token = self.token(client)
        res = client.post("/register", data={"username": "x", "password": PASSWORD, "csrf_token": token})
        self.assertIn("/login", res.headers["Location"])
        self.assertEqual(client.get("/").status_code, 302)

    def test_login_logout(self):
        self.signup()
        self.assertEqual(self.post("/logout").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)
        token = self.token()
        bad = self.client.post("/login", data={"username": "ahmad", "password": "nope", "csrf_token": token})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post("/login", data={"username": "ahmad", "password": PASSWORD, "csrf_token": token})
        self.assertEqual(good.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_background_requests_keep_the_csrf_token(self):
        html = self.client.get("/register").get_data(as_text=True)
        token = re.search(r'name="csrf" content="(\w+)"', html).group(1)
        for url in ("/favicon.ico", "/apple-touch-icon.png", "/"):  # sent by browsers on their own
            self.client.get(url)
        res = self.client.post("/register", data={"username": "a", "password": PASSWORD, "csrf_token": token})
        self.assertEqual(res.status_code, 302)

    def test_deleted_user_loses_access(self):
        self.signup()
        execute(self.cfg.db, "DELETE FROM users")
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/trash").status_code, 302)

    def test_password_is_hashed(self):
        self.signup()
        self.assertNotIn(PASSWORD.encode(), sh("cat", self.cfg.db).stdout)

    def test_unauthorized_blocked(self):
        self.signup()
        anon = self.app.test_client()
        self.upload("secret.txt", b"secret")
        for url in ("/", "/trash", "/preview?path=secret.txt", "/raw?path=secret.txt",
                    "/download?path=secret.txt", "/static/../secret.txt"):
            self.assertIn(anon.get(url).status_code, (302, 404), url)
        self.assertEqual(anon.get("/api/du").status_code, 401)
        self.assertEqual(anon.post("/api/op", json={"action": "delete"}).status_code, 403)

    def test_csrf_required(self):
        self.signup()
        res = self.client.post("/api/op", json={"action": "add_file", "items": ["a"]})
        self.assertEqual(res.status_code, 403)
        self.assertFalse(self.exists("a"))

    def test_login_rate_limit(self):
        self.signup()
        client = self.app.test_client()
        token = self.token(client)
        data = {"username": "ahmad", "password": "wrong", "csrf_token": token}
        codes = [client.post("/login", data=data).status_code for _ in range(6)]
        self.assertEqual(codes, [401] * 5 + [429])
        data["password"] = PASSWORD
        self.assertEqual(client.post("/login", data=data).status_code, 429)

    def test_session_cookie_flags_and_headers(self):
        token = self.token()
        res = self.client.post("/register", data={"username": "a", "password": PASSWORD, "csrf_token": token})
        cookie = res.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("default-src 'self'", res.headers["Content-Security-Policy"])


class ListingTests(Base):
    def setUp(self):
        super().setUp()
        self.signup()

    def test_listing_shows_only_required_columns(self):
        self.upload("File.txt", b"x" * 2048)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("File.txt", html)
        self.assertIn("Total (ls):", html)
        self.assertIn("Folder disk usage", html)
        self.assertNotIn("rw-r", html)
        self.assertNotIn(os.environ.get("USER", "no-such-user-x") + " ", html)

    def test_listing_runs_ls_lh(self):
        with self.assertLogs("barefile.cli", "INFO") as logs:
            logging.disable(logging.NOTSET)
            self.client.get("/")
            seen = " ".join(logs.output)
        logging.disable(logging.CRITICAL)
        self.assertRegex(seen, r"/ls', '-lhnA'")

    def test_parse_ls_formats(self):
        total, rows = parse_ls("total 16M\n"
                               "-rw-r--r-- 1 501 20 700K Oct 30 11:44 File name.txt\n"
                               "drwxr-xr-x@ 2 501 20 4.0K Oct 30  2025 Photos\n"
                               "lrwxr-xr-x 1 501 20 4B Oct 30 11:44 link -> /etc\n")
        self.assertEqual(total, "16M")
        self.assertEqual(rows["File name.txt"], ("700K", "Oct 30 11:44"))
        self.assertEqual(rows["Photos"], ("4.0K", "Oct 30 2025"))
        self.assertIn("link", rows)
        self.assertEqual(parse_ls("total 6008\n")[0], "6008 blocks")

    def test_unusual_filenames(self):
        names = ["a b.txt", "ünï çødé.txt", "日本語.txt", "--dash", "pipe|name", "tab\tname", "new\nline",
                 "cr\rname", " -> x", "quote'\".txt", "%s.txt"]
        for name in names:
            sh("touch", f"{self.root}/{name}")
        store = Storage(self.cfg, CLI(self.cfg))
        result = store.list("")
        self.assertEqual(sorted(e["name"] for e in result["entries"]), sorted(names))
        self.assertTrue(all(e["size_h"] and e["date"] for e in result["entries"]))
        # the risky names survive a full operation, not just display
        self.op("delete", ["new\nline", "--dash"])
        self.assertFalse(self.exists("new\nline") or self.exists("--dash"))

    def test_owner_and_group_are_not_rendered(self):
        self.write("x", b"1")
        res = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(">wheel<", res)

    def test_sorting(self):
        self.write("b", b"x" * 300)
        self.write("A", b"x" * 20)
        self.write("c", b"x" * 1000)
        sh("touch", "-t", "202001010000", f"{self.root}/c")
        sh("touch", "-t", "202101010000", f"{self.root}/A")
        sh("touch", "-t", "202201010000", f"{self.root}/b")
        self.assertEqual(self.names(sort="name"), ["A", "b", "c"])
        self.assertEqual(self.names(sort="name", order="desc"), ["c", "b", "A"])
        self.assertEqual(self.names(sort="size"), ["A", "b", "c"])
        self.assertEqual(self.names(sort="size", order="desc"), ["c", "b", "A"])
        self.assertEqual(self.names(sort="date"), ["c", "A", "b"])
        self.assertEqual(self.names(sort="date", order="desc"), ["b", "A", "c"])

    def test_size_sort_is_numeric_not_human(self):
        self.write("nine", b"x" * 9)
        self.write("big", b"x" * 2048)  # "2.0K" would sort before "9B" as text
        self.assertEqual(self.names(sort="size"), ["nine", "big"])

    def test_du(self):
        sh("mkdir", f"{self.root}/d")
        sh("dd", "if=/dev/zero", f"of={self.root}/d/big", "bs=1024", "count=2048")
        size = self.client.get("/api/du").get_json()["du"]
        self.assertRegex(size, r"^2\.\d*M$|^2M$")

    def lock_subfolder(self):
        sh("mkdir", "-p", f"{self.root}/open", f"{self.root}/locked")
        self.write("open/found.txt", b"x" * 5000)
        sh("chmod", "000", f"{self.root}/locked")
        self.addCleanup(sh, "chmod", "755", f"{self.root}/locked")

    def test_du_reports_partial_size_for_unreadable_subfolder(self):
        self.lock_subfolder()
        size = self.client.get("/api/du").get_json().get("du", "")
        self.assertRegex(size, r"^\S+ \(unreadable folders skipped\)$")

    def test_search_survives_unreadable_subfolder(self):
        self.lock_subfolder()
        hit = self.client.get("/", query_string={"q": "found"})
        miss = self.client.get("/", query_string={"q": "no-such-name"})
        self.assertIn("found.txt", hit.get_data(as_text=True))
        self.assertEqual(miss.status_code, 200)
        self.assertIn("No matches", miss.get_data(as_text=True))

    def test_search(self):
        sh("mkdir", "-p", f"{self.root}/deep/er")
        sh("touch", f"{self.root}/deep/er/Report 2024.PDF", f"{self.root}/other.txt")
        sh("mkdir", f"{self.root}/report-dir")
        html = self.client.get("/", query_string={"q": "report"}).get_data(as_text=True)
        self.assertIn("Report 2024.PDF", html)
        self.assertIn("/deep/er", html)
        self.assertIn("report-dir/", html)
        self.assertNotIn("other.txt", html)
        self.assertNotIn(self.root, html)

    def test_search_treats_glob_as_literal(self):
        sh("touch", f"{self.root}/plain.txt")
        html = self.client.get("/", query_string={"q": "*"}).get_data(as_text=True)
        self.assertNotIn("plain.txt", html)

    def test_search_does_not_follow_symlinks(self):
        outside = f"{self.base}/outside"
        sh("mkdir", outside)
        sh("touch", f"{outside}/needle.txt")
        sh("ln", "-s", outside, f"{self.root}/link")
        html = self.client.get("/", query_string={"q": "needle"}).get_data(as_text=True)
        self.assertIn("No matches", html)


class FileOpTests(Base):
    def setUp(self):
        super().setUp()
        self.signup()

    def test_add_file_and_folder(self):
        self.op("add_file", ["new.txt"], path="")
        self.op("add_folder", ["New Folder"], path="")
        self.assertTrue(self.exists("new.txt"))
        self.assertTrue(os.path.isdir(f"{self.root}/New Folder"))

    def test_invalid_names_rejected(self):
        for name in ("", ".", "..", "a/b", "x\x01y", "x\ny", "a" * 256):
            _, end = self.op("add_file", [name], path="")
            self.assertEqual(end["failed"], 1, repr(name))
        self.assertEqual(sh("ls", "-A", self.root).stdout, b"")

    def test_collisions_rejected(self):
        self.upload("a.txt", b"original")
        sh("mkdir", f"{self.root}/d")
        self.write("d/a.txt", b"other")
        self.write("b.txt", b"b")
        for action, items, extra in (("add_file", ["a.txt"], {"path": ""}),
                                     ("add_folder", ["a.txt"], {"path": ""}),
                                     ("rename", ["b.txt"], {"name": "a.txt"}),
                                     ("copy", ["a.txt"], {"dest": "d"}),
                                     ("move", ["a.txt"], {"dest": "d"})):
            lines, end = self.op(action, items, **extra)
            self.assertEqual(end["failed"], 1, action)
            self.assertIn("Already exists", lines[0])
        self.assertEqual(self.upload("a.txt", b"new").status_code, 409)
        self.assertEqual(self.read("a.txt"), b"original")
        self.assertEqual(self.read("d/a.txt"), b"other")
        self.assertEqual(self.read("b.txt"), b"b")

    def test_rename(self):
        self.upload("a.txt", b"1")
        self.op("rename", ["a.txt"], name="b c.txt")
        self.assertEqual(self.read("b c.txt"), b"1")
        self.assertFalse(self.exists("a.txt"))
        _, end = self.op("rename", ["b c.txt"], name="../escape")
        self.assertEqual(end["failed"], 1)

    def test_copy_file_and_folder_verbose(self):
        sh("mkdir", "-p", f"{self.root}/src/inner", f"{self.root}/dst")
        self.write("src/inner/f.txt", b"hi")
        lines, end = self.op("copy", ["src"], dest="dst")
        self.assertEqual(end["done"], 1)
        self.assertEqual(self.read("dst/src/inner/f.txt"), b"hi")
        self.assertEqual(self.read("src/inner/f.txt"), b"hi")
        self.assertTrue(any("f.txt" in line for line in lines), lines)
        self.assertFalse(any(self.base in line for line in lines), lines)
        self.assertEqual(self.tmp_files(), b"")

    def test_copy_into_itself_rejected(self):
        sh("mkdir", "-p", f"{self.root}/a/b")
        for dest in ("a", "a/b"):
            _, end = self.op("copy", ["a"], dest=dest)
            self.assertEqual(end["failed"], 1)
            _, end = self.op("move", ["a"], dest=dest)
            self.assertEqual(end["failed"], 1)
        self.assertEqual(sh("find", f"{self.root}/a", "-mindepth", "1").stdout.count(b"\n"), 1)

    def test_move(self):
        sh("mkdir", f"{self.root}/d", f"{self.root}/sub")
        self.write("sub/f", b"x")
        self.write("g", b"y")
        _, end = self.op("move", ["sub", "g"], dest="d")
        self.assertEqual((end["done"], end["failed"], end["total"]), (2, 0, 2))
        self.assertEqual(self.read("d/sub/f"), b"x")
        self.assertEqual(self.read("d/g"), b"y")
        self.assertFalse(self.exists("sub") or self.exists("g"))

    def test_multi_select_partial_failure_is_counted(self):
        sh("mkdir", f"{self.root}/d")
        self.write("a", b"1")
        self.write("b", b"2")
        self.write("d/b", b"exists")
        _, end = self.op("copy", ["a", "b"], dest="d")
        self.assertEqual((end["done"], end["failed"]), (1, 1))
        self.assertEqual(self.read("d/a"), b"1")

    def test_upload_and_download(self):
        body = os.urandom(200_000)
        self.assertEqual(self.upload("blob ünï.bin", body).status_code, 200)
        self.assertEqual(self.read("blob ünï.bin"), body)
        res = self.client.get("/download", query_string={"path": "blob ünï.bin"})
        self.assertEqual(res.get_data(), body)
        self.assertEqual(res.headers["Content-Length"], str(len(body)))
        self.assertIn("attachment; filename*=UTF-8''blob%20%C3%BCn%C3%AF.bin", res.headers["Content-Disposition"])
        self.assertEqual(self.tmp_files(), b"")

    def test_multiple_uploads(self):
        for index in range(3):
            self.assertEqual(self.upload(f"f{index}", b"x").status_code, 200)
        self.assertEqual(self.names(), ["f0", "f1", "f2"])

    def test_range_requests(self):
        body = bytes(range(256)) * 1000  # spans several 64K dd blocks
        self.upload("video.mp4", body)
        for spec, start, stop in (("bytes=0-99", 0, 100), ("bytes=70000-200000", 70000, 200001),
                                  (f"bytes=65536-{65536 + 9}", 65536, 65546), ("bytes=-10", len(body) - 10, len(body))):
            res = self.client.get("/raw?path=video.mp4", headers={"Range": spec})
            self.assertEqual(res.status_code, 206, spec)
            self.assertEqual(res.get_data(), body[start:stop], spec)
            self.assertEqual(res.headers["Content-Range"], f"bytes {start}-{stop - 1}/{len(body)}")
        res = self.client.get("/raw?path=video.mp4", headers={"Range": f"bytes={len(body)}-"})
        self.assertEqual(res.status_code, 416)

    def test_interrupted_upload_leaves_nothing(self):
        res = self.client.post("/api/upload", query_string={"path": "", "name": "partial.bin"},
                               input_stream=io.BytesIO(b"only-part"), headers={"X-CSRF-Token": self.csrf},
                               environ_overrides={"CONTENT_LENGTH": "1000"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.exists("partial.bin"))
        self.assertEqual(self.tmp_files(), b"")

    def test_upload_requires_length(self):
        res = self.client.post("/api/upload", query_string={"name": "x"}, input_stream=io.BytesIO(b"x"),
                               headers={"X-CSRF-Token": self.csrf, "Transfer-Encoding": "chunked"})
        self.assertEqual(res.status_code, 411)

    def test_preview_kinds(self):
        self.upload("a.png", b"\x89PNG")
        self.upload("a.pdf", b"%PDF-1.4")
        self.upload("a.mp4", b"x")
        self.upload("a.txt", b"<b>hello</b>")
        self.upload("noext", b"plain words\n")
        body = lambda name: self.client.get("/preview", query_string={"path": name}).get_data(as_text=True)  # noqa: E731
        self.assertIn("<img", body("a.png"))
        self.assertIn("<iframe", body("a.pdf"))
        self.assertIn("<video", body("a.mp4"))
        self.assertIn("&lt;b&gt;hello", body("a.txt"))
        self.assertIn("plain words", body("noext"))

    def test_html_and_svg_never_render_inline(self):
        self.upload("x.html", b"<script>alert(1)</script>")
        self.upload("x.svg", b"<svg onload='alert(1)'/>")
        for name in ("x.html", "x.svg"):
            res = self.client.get("/raw", query_string={"path": name})
            self.assertTrue(res.headers["Content-Disposition"].startswith("attachment"), name)
            self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")


class TrashTests(Base):
    def setUp(self):
        super().setUp()
        self.signup()

    def rows(self):
        return self.client.get("/trash").get_data(as_text=True)

    def ids(self):
        return [int(i) for i in re.findall(r'data-op="restore" data-item="(\d+)"', self.rows())]

    def test_delete_restore(self):
        sh("mkdir", "-p", f"{self.root}/d/sub")
        self.write("d/sub/f", b"keep")
        self.op("delete", ["d"])
        self.assertFalse(self.exists("d"))
        self.assertNotIn(b"[", sh("ls", self.cfg.trash).stdout)
        self.assertEqual(self.names(), [])
        self.op("restore", self.ids())
        self.assertEqual(self.read("d/sub/f"), b"keep")
        self.assertEqual(self.ids(), [])

    def test_duplicate_names(self):
        for body in (b"one", b"two"):
            self.upload("same.txt", body)
            self.op("delete", ["same.txt"])
        self.assertEqual(len(self.ids()), 2)
        newest, oldest = self.ids()
        self.op("restore", [oldest])
        self.assertEqual(self.read("same.txt"), b"one")
        _, end = self.op("restore", [newest])  # slot is taken: must not overwrite
        self.assertEqual(end["failed"], 1)
        self.assertEqual(self.read("same.txt"), b"one")
        self.assertEqual(self.ids(), [newest])

    def test_restore_recreates_missing_parent(self):
        sh("mkdir", "-p", f"{self.root}/gone/deep")
        self.write("gone/deep/f", b"x")
        self.op("delete", ["gone/deep/f"])
        self.op("delete", ["gone"])
        item = self.ids()[1]  # the file, deleted first
        self.op("restore", [item])
        self.assertEqual(self.read("gone/deep/f"), b"x")

    def test_trash_is_outside_storage(self):
        self.assertFalse(self.cfg.trash.startswith(self.root))

    def test_permanent_delete_needs_confirmation(self):
        self.upload("f", b"x")
        self.op("delete", ["f"])
        res = self.post("/api/op", json={"action": "purge", "items": self.ids()})
        self.assertEqual(res.status_code, 400)
        res = self.post("/api/op", json={"action": "empty"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(len(self.ids()), 1)

    def test_permanent_delete(self):
        sh("mkdir", f"{self.root}/d")
        self.write("d/f", b"x")
        self.upload("g", b"y")
        self.op("delete", ["d", "g"])
        lines, end = self.op("purge", self.ids(), confirm=True)
        self.assertEqual(end["done"], 2)
        self.assertTrue(lines, "rm -v output expected")
        self.assertEqual(sh("ls", "-A", self.cfg.trash).stdout, b"")
        self.assertEqual(self.ids(), [])

    def test_empty_trash(self):
        for name in ("a", "b", "c"):
            self.upload(name, b"x")
        self.op("delete", ["a", "b", "c"])
        _, end = self.op("empty", confirm=True)
        self.assertEqual((end["done"], end["total"]), (3, 3))
        self.assertEqual(sh("ls", "-A", self.cfg.trash).stdout, b"")


class SecurityTests(Base):
    def setUp(self):
        super().setUp()
        self.signup()
        self.outside = f"{self.base}/outside"
        sh("mkdir", self.outside)
        self.secret = f"{self.outside}/secret.txt"
        subprocess.run(["dd", f"of={self.secret}"], input=b"top secret", capture_output=True,
                       env={"PATH": CLI_PATH})

    def test_traversal_rejected(self):
        for path in ("..", "../..", "../outside", "a/../../outside", "../outside/secret.txt", "/../outside"):
            self.assertEqual(self.client.get("/", query_string={"path": path}).status_code, 403, path)
            self.assertEqual(self.client.get("/download", query_string={"path": path}).status_code, 403, path)
            self.assertEqual(self.upload("x", b"x", path=path).status_code, 403, path)
            _, end = self.op("delete", [path])
            self.assertEqual(end["failed"], 1, path)
        self.assertTrue(os.path.exists(self.secret))

    def test_absolute_path_stays_inside_root(self):
        self.assertEqual(self.client.get("/download", query_string={"path": self.secret}).status_code, 404)

    def test_root_cannot_be_changed(self):
        for action, extra in (("delete", {}), ("rename", {"name": "x"}), ("move", {"dest": "/"})):
            for root in ("", "/", "."):
                _, end = self.op(action, [root], **extra)
                self.assertEqual(end["failed"], 1, (action, root))
        self.assertTrue(os.path.isdir(self.root))

    def test_symlink_escape_blocked(self):
        sh("ln", "-s", self.outside, f"{self.root}/link")
        sh("ln", "-s", self.secret, f"{self.root}/file-link")
        for url, path in (("/", "link"), ("/", "link/."), ("/download", "file-link"), ("/download", "link/secret.txt"),
                          ("/raw", "file-link"), ("/preview", "file-link")):
            self.assertEqual(self.client.get(url, query_string={"path": path}).status_code, 403, (url, path))
        self.assertEqual(self.upload("planted", b"x", path="link").status_code, 403)
        self.assertEqual(self.client.get("/api/du", query_string={"path": "link"}).status_code, 403)
        _, end = self.op("add_file", ["planted"], path="link")
        self.assertEqual(end["failed"], 1)
        _, end = self.op("copy", ["link/secret.txt"], dest="")
        self.assertEqual(end["failed"], 1)
        self.upload("a", b"a")
        _, end = self.op("move", ["a"], dest="link")
        self.assertEqual(end["failed"], 1)
        self.assertEqual(sh("ls", self.outside).stdout, b"secret.txt\n")

    def test_copying_a_symlink_copies_the_link_not_the_target(self):
        sh("ln", "-s", self.secret, f"{self.root}/file-link")
        sh("mkdir", f"{self.root}/d")
        self.op("copy", ["file-link"], dest="d")
        self.assertTrue(os.path.islink(f"{self.root}/d/file-link"))
        self.assertEqual(self.client.get("/download", query_string={"path": "d/file-link"}).status_code, 403)

    def test_deleting_a_symlink_keeps_its_target(self):
        sh("ln", "-s", self.outside, f"{self.root}/link")
        self.op("delete", ["link"])
        self.assertFalse(self.exists("link"))
        self.assertTrue(os.path.exists(self.secret))
        self.op("empty", confirm=True)
        self.assertTrue(os.path.exists(self.secret))

    def test_nul_byte_rejected(self):
        res = self.client.get("/", query_string={"path": "a\0b"})
        self.assertEqual(res.status_code, 403)

    def test_error_pages_hide_internal_paths(self):
        res = self.client.get("/?path=../x")
        self.assertNotIn(self.base, res.get_data(as_text=True))


class SetRootTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = os.path.realpath(tmp.name)
        self.app = f"{self.base}/app"
        sh("mkdir", "-p", f"{self.app}/scripts", f"{self.base}/data", f"{self.base}/old", f"{self.base}/my new")
        sh("cp", f"{config.BASE}/scripts/set-root.sh", f"{self.app}/scripts/")
        sh("cp", *[f"{config.BASE}/{name}.py" for name in ("cli", "config", "database", "files")], self.app)
        sh("ln", "-s", f"{config.BASE}/.venv", f"{self.app}/.venv")

    def run_it(self, env, *args):
        with open(f"{self.app}/.env", "w") as f:
            f.write(env)
        proc = subprocess.run([f"{self.app}/scripts/set-root.sh", *args], cwd=self.base,
                              capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        with open(f"{self.app}/.env") as f:
            return proc, f.read()

    def env(self, root_line):
        return f"FILEMANAGER_SECRET_KEY=s\n{root_line}\nFILEMANAGER_DATA_DIR={self.base}/data\n"

    def test_replaces_active_line_and_keeps_the_rest(self):
        proc, text = self.run_it(self.env(f"FILEMANAGER_STORAGE_ROOT={self.base}/old"), "my new")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(text, self.env(f"FILEMANAGER_STORAGE_ROOT='{self.base}/my new'"))
        sourced = subprocess.run(["bash", "-c", f". {self.app}/.env; echo \"$FILEMANAGER_STORAGE_ROOT\""],
                                 capture_output=True, text=True).stdout.strip()
        self.assertEqual(sourced, f"{self.base}/my new")

    def test_fills_in_commented_line(self):
        proc, text = self.run_it(self.env("#FILEMANAGER_STORAGE_ROOT=./data/storage"), "old")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(text, self.env(f"FILEMANAGER_STORAGE_ROOT={self.base}/old"))

    def test_refused_folders_leave_env_untouched(self):
        start = self.env(f"FILEMANAGER_STORAGE_ROOT={self.base}/old")
        for folder in (f"{self.base}/missing", "/", self.base):  # base contains the data folder
            proc, text = self.run_it(start, folder)
            self.assertNotEqual(proc.returncode, 0, folder)
            self.assertEqual(text, start)


class ReadOnlyDefaultTests(unittest.TestCase):
    def load(self, **env):
        base = {"FILEMANAGER_SECRET_KEY": "s"}
        with unittest.mock.patch.dict(os.environ, {**base, **env}, clear=True):
            return config.load().read_only

    def test_read_only_unless_turned_off(self):
        self.assertTrue(self.load())
        self.assertTrue(self.load(FILEMANAGER_READ_ONLY="1"))
        self.assertFalse(self.load(FILEMANAGER_READ_ONLY="0"))

    def test_example_env_is_read_only(self):
        with open(os.path.join(config.BASE, ".env.example")) as f:
            self.assertIn("\nFILEMANAGER_READ_ONLY=1\n", f.read())


class ReadOnlyTests(Base):
    read_only = True

    def setUp(self):
        super().setUp()
        self.signup()
        sh("mkdir", f"{self.root}/d")
        self.write("f.txt", b"keep")

    def test_every_change_is_refused(self):
        for body in ({"action": "add_file", "items": ["n"], "path": ""},
                     {"action": "add_folder", "items": ["n"], "path": ""},
                     {"action": "rename", "items": ["f.txt"], "name": "n"},
                     {"action": "copy", "items": ["f.txt"], "dest": "d"},
                     {"action": "move", "items": ["f.txt"], "dest": "d"},
                     {"action": "delete", "items": ["f.txt"]},
                     {"action": "restore", "items": [1]},
                     {"action": "purge", "items": [1], "confirm": True},
                     {"action": "empty", "confirm": True}):
            res = self.post("/api/op", json=body)
            self.assertEqual(res.status_code, 403, body)
        self.assertEqual(self.upload("n", b"x").status_code, 403)
        self.assertEqual(sorted(self.names_on_disk()), ["d", "f.txt"])
        self.assertEqual(self.read("f.txt"), b"keep")

    def names_on_disk(self):
        return sh("ls", "-A", self.root).stdout.decode().split()

    def test_reading_still_works(self):
        page = self.client.get("/", query_string={"sort": "size"}).get_data(as_text=True)
        self.assertIn("f.txt", page)
        self.assertEqual(self.client.get("/download?path=f.txt").get_data(), b"keep")
        self.assertIn("f.txt", self.client.get("/?q=f.txt").get_data(as_text=True))
        self.assertTrue(self.client.get("/api/du").get_json()["du"])
        self.assertEqual(self.client.get("/trash").status_code, 200)

    def test_change_controls_are_hidden(self):
        page = self.client.get("/").get_data(as_text=True)
        for control in ('id="add-file"', 'id="delete"', 'class="select"', "data-rename"):
            self.assertNotIn(control, page)
        self.assertIn("Read-only", page)


class CliTests(Base):
    def setUp(self):
        super().setUp()
        self.cli = CLI(self.cfg)

    def test_disallowed_command(self):
        with self.assertRaises(CommandError):
            self.cli.run("bash", "-c", "true")

    def test_disallowed_option(self):
        with self.assertRaises(CommandError):
            self.cli.run("rm", "-rf", "--no-preserve-root", "/")
        with self.assertRaises(CommandError):
            self.cli.run("find", "/", "-delete")

    def test_relative_path_after_double_dash_rejected(self):
        with self.assertRaises(CommandError):
            self.cli.run("rm", "-v", "--", "relative")

    def test_failure_is_readable_and_scrubbed(self):
        with self.assertRaises(CommandError) as caught:
            self.cli.run("ls", "-lhnA", "--", f"{self.root}/missing")
        message = str(caught.exception)
        self.assertIn("No such file", message)
        self.assertNotIn(self.base, message)

    def test_timeout(self):
        sh("mkfifo", f"{self.base}/pipe")
        with self.assertRaisesRegex(CommandError, "timed out"):
            self.cli.run("head", "-c", "1", "--", f"{self.base}/pipe", timeout=0.2)

    def test_stream_failure_raises_after_output(self):
        with self.assertRaises(CommandError):
            list(self.cli.stream("rm", "-v", "--", f"{self.root}/missing"))

    def test_failing_command_is_reported_to_the_client(self):
        self.signup()
        self.upload("f", b"x")
        sh("chmod", "555", self.root)
        self.addCleanup(sh, "chmod", "755", self.root)
        _, end = self.op("delete", ["f"])
        self.assertEqual(end["failed"], 1)
        self.assertEqual(self.ids_in_trash(), 0)  # the row is rolled back
        self.assertTrue(self.exists("f"))

    def ids_in_trash(self):
        return self.client.get("/trash").get_data(as_text=True).count('data-op="restore"')

    def test_validate_name(self):
        self.assertEqual(validate_name("ok name.txt"), "ok name.txt")
        with self.assertRaises(FileError):
            validate_name("\udc80")


class PlatformTests(Base):
    def test_flavour_probe_matches_binary(self):
        cli = CLI(self.cfg)
        result = subprocess.run([cli.bins["stat"], "--version"], capture_output=True, env=cli.env)
        self.assertEqual(cli.gnu, result.returncode == 0)
        self.assertEqual(cli.mv_flags, "-vnT" if cli.gnu else "-vn")
        self.assertEqual(cli.stat_args("m")[0], "--printf" if cli.gnu else "-f")

    def test_only_binaries_on_the_pinned_path_run(self):
        cli = CLI(self.cfg)
        roots = tuple(CLI_PATH.split(":"))
        self.assertTrue(all(path.startswith(roots) for path in cli.bins.values()))

    def test_refuses_unsafe_configuration(self):
        with self.assertRaises(SystemExit):
            config.Config("/", f"{self.base}/app", "s")
        with self.assertRaises(SystemExit):
            config.Config(self.root, f"{self.root}/app", "s")
        with self.assertRaises(SystemExit):
            config.Config(self.root, f"{self.base}/app", "")

    def test_bad_cli_path_fails_fast(self):
        self.cfg.cli_path = f"{self.base}/{uuid.uuid4().hex}"
        with self.assertRaises(SystemExit):
            CLI(self.cfg)


if __name__ == "__main__":
    unittest.main()
