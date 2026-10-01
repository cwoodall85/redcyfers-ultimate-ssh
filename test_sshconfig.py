#!/usr/bin/env python3
"""Headless tests for the config writer.

Runs against tests/fixture-config.txt -- a synthetic config with invented
hosts. No real inventory here, and nothing touches ~/.ssh/config.

The bar: an untouched config must render byte-for-byte identically, and any
single edit must produce a minimal diff.
"""

import os
import sys
import shutil
import difflib
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sshconfig
from sshconfig import SshConfig, ConfigError, ConflictError

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "tests", "fixture-config.txt")
FAILURES = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def diff(a, b):
    return [ln for ln in difflib.unified_diff(
        a.split("\n"), b.split("\n"), lineterm="", n=0)
        if not ln.startswith(("---", "+++", "@@"))]


def fresh():
    """A scratch copy of the fixture, with the 0600 a real config would have.

    git does not track permission bits beyond the exec flag, so the mode is
    set here rather than assumed from the checked-in fixture.
    """
    tmp = tempfile.mktemp(suffix=".sshconfig")
    shutil.copy2(FIXTURE, tmp)
    os.chmod(tmp, 0o600)
    return tmp


def main():
    original = open(FIXTURE, errors="replace").read()

    print("\n== round trip ==")
    cfg = SshConfig.load(FIXTURE)
    check("renders byte-for-byte identically", cfg.render() == original,
          f"{len(diff(original, cfg.render()))} differing lines")
    hosts = cfg.hosts()
    groups = cfg.groups()
    check("host count", len(hosts) == 16, str(len(hosts)))
    check("group count", len(groups) == 6, str(len(groups)))
    check("Host * excluded", all(a != "*" for a in (h.alias for h in hosts)))
    check("Host * still in the file", "\nHost *\n" in cfg.render())
    check("groups in file order",
          list(groups)[:3] == ["AWS us-west-2", "Admin", "Web tier"],
          str(list(groups)[:3]))

    print("\n== edit one host ==")
    cfg = SshConfig.load(FIXTURE)
    view = cfg.find("lab-web2")
    cfg.update_host(view, options=[("Port", "2222"), ("User", "deploy")])
    d = diff(original, cfg.render())
    check("only the edited stanza changes", len(d) <= 6, f"{len(d)} lines: {d[:8]}")
    check("Port written", "Port 2222" in cfg.render())
    check("other hosts untouched", "Host lab-web3" in cfg.render())

    print("\n== rename a host ==")
    cfg = SshConfig.load(FIXTURE)
    cfg.update_host(cfg.find("lab-web2"), alias="lab-web2-renamed")
    out = cfg.render()
    check("new alias present", "Host lab-web2-renamed" in out)
    check("old alias gone", "\nHost lab-web2\n" not in out)
    check("its HostName survived the rewrite",
          cfg.find("lab-web2-renamed").hostname == "10.10.1.11",
          cfg.find("lab-web2-renamed").hostname)

    print("\n== add a host ==")
    cfg = SshConfig.load(FIXTURE)
    cfg.add_host("lab-web-new", [("HostName", "10.10.1.99"),
                                  ("User", "root")], "Web tier")
    check("lands in the requested group",
          cfg.find("lab-web-new").group == "Web tier",
          cfg.find("lab-web-new").group)
    check("group grew by one", len(cfg.groups()["Web tier"]) == 4,
          str(len(cfg.groups()["Web tier"])))
    check("other groups unchanged", len(cfg.groups()["Services"]) == 3)
    check("duplicate alias rejected",
          rejects(cfg.add_host, "lab-web1", [], "Web tier"))
    check("whitespace alias rejected",
          rejects(cfg.add_host, "bad name", [], "Web tier"))
    check("wildcard alias rejected",
          rejects(cfg.add_host, "web-*", [], "Web tier"))

    print("\n== delete a host ==")
    cfg = SshConfig.load(FIXTURE)
    cfg.delete_host(cfg.find("lab-web2"))
    out = cfg.render()
    check("host removed", cfg.find("lab-web2") is None)
    check("neighbours intact", cfg.find("lab-web1") and cfg.find("lab-web3"))
    check("no stanza fragment left behind", "10.10.1.11" not in out)
    d = diff(original, out)
    check("deletion diff is minimal", len(d) <= 6, f"{len(d)} lines")

    print("\n== delete the LAST host of a group ==")
    # the regression this parser was designed against: if a stanza swallowed
    # unindented lines, deleting the last host would take the next banner too
    cfg = SshConfig.load(FIXTURE)
    cdn = cfg.groups()["CDN"]
    for view in list(cdn):
        cfg.delete_host(view)
    out = cfg.render()
    check("following banner survives", "# --- Services ---" in out)
    check("own banner survives", "# --- CDN ---" in out)
    check("group now empty but declared", cfg.groups().get("CDN") == [],
          str(cfg.groups().get("CDN")))
    check("following group intact", len(cfg.groups()["Services"]) == 3)

    print("\n== categories ==")
    cfg = SshConfig.load(FIXTURE)
    cfg.rename_group("Web tier", "Web servers")
    check("banner renamed", "# --- Web servers ---" in cfg.render())
    check("members follow the rename",
          len(cfg.groups()["Web servers"]) == 3,
          str(len(cfg.groups().get("Web servers", []))))
    check("old name gone", "Web tier" not in cfg.groups())
    check("duplicate category rejected",
          rejects(cfg.rename_group, "Services", "Admin"))

    cfg = SshConfig.load(FIXTURE)
    cfg.add_group("Kubernetes")
    check("category added", "Kubernetes" in cfg.groups())
    cfg.add_host("k8s-01", [("HostName", "10.10.9.1")], "Kubernetes")
    check("host added to new category",
          [h.alias for h in cfg.groups()["Kubernetes"]] == ["k8s-01"])
    check("duplicate category rejected", rejects(cfg.add_group, "Services"))

    print("\n== move hosts / delete a category ==")
    cfg = SshConfig.load(FIXTURE)
    cfg.move_host(cfg.find("lab-web2"), "Services")
    check("host moved", cfg.find("lab-web2").group == "Services")
    check("source group shrank", len(cfg.groups()["Web tier"]) == 2)
    check("target group grew", len(cfg.groups()["Services"]) == 4)
    check("host body came along",
          cfg.find("lab-web2").hostname == "10.10.1.11")

    cfg = SshConfig.load(FIXTURE)
    cfg.delete_group("CDN", move_to="Services")
    check("category gone", "CDN" not in cfg.groups())
    check("its hosts relocated", len(cfg.groups()["Services"]) == 5,
          str(len(cfg.groups()["Services"])))
    check("banner removed", "# --- CDN ---" not in cfg.render())

    cfg = SshConfig.load(FIXTURE)
    cfg.delete_group("CDN")
    check("delete without move drops the hosts",
          cfg.find("lab-cdn0") is None and "CDN" not in cfg.groups())

    print("\n== wildcard defaults that shadow every host ==")
    cfg = SshConfig.load(FIXTURE)
    check("Host * above the hosts is detected as shadowing",
          "User" in cfg.shadowed_keys(), str(sorted(cfg.shadowed_keys())))
    before_hosts = len(cfg.hosts())
    before_groups = list(cfg.groups())
    moved = cfg.move_defaults_to_end()
    check("the wildcard block was moved", moved == 1, str(moved))
    out = cfg.render()
    tmp2 = tempfile.mktemp(suffix=".cfg")
    open(tmp2, "w").write(out)
    again = SshConfig.load(tmp2)
    check("shadowing is gone after the move", again.shadowed_keys() == set(),
          str(sorted(again.shadowed_keys())))
    check("every host survived", len(again.hosts()) == before_hosts,
          f"{len(again.hosts())} vs {before_hosts}")
    check("categories unchanged", list(again.groups()) == before_groups)
    check("Host * is still present", "\nHost *\n" in out)
    check("it now sits after the hosts",
          out.index("Host *") > out.index("Host lab-dev02"))
    check("its contents were not rewritten",
          "    User deploy" in out and "    ServerAliveInterval 60" in out)
    check("a second run is a no-op", again.move_defaults_to_end() == 0)
    os.unlink(tmp2)

    print("\n== save safety ==")
    path = fresh()
    try:
        cfg = SshConfig.load(path)
        cfg.update_host(cfg.find("lab-web2"), options=[("Port", "2222")])
        backup_dir = tempfile.mkdtemp()
        backup = cfg.save(backup_dir=backup_dir)
        check("backup written", backup and os.path.exists(backup))
        check("backup matches pre-edit content",
              open(backup).read() == original)
        check("saved file has the edit", "Port 2222" in open(path).read())
        check("permissions preserved 0600",
              oct(os.stat(path).st_mode)[-3:] == "600",
              oct(os.stat(path).st_mode)[-3:])
        check("no temp file left behind", not os.path.exists(path + ".ultimate-ssh-tmp"))
        check("reload sees the edit",
              SshConfig.load(path).find("lab-web2").block.get("Port") == "2222")

        # someone edits the file in vim while the manager is open
        cfg2 = SshConfig.load(path)
        with open(path, "a") as fh:
            fh.write("\n# touched externally\n")
        cfg2.update_host(cfg2.find("lab-web1"), options=[("Port", "999")])
        check("refuses to clobber an external edit",
              rejects(cfg2.save, backup_dir=backup_dir), "ConflictError")
        check("external edit still there", "touched externally" in open(path).read())

        # a rejected save must not have written anything
        cfg3 = SshConfig.load(path)
        before = open(path).read()
        cfg3.add_host("dupe-test", [], "Services")
        cfg3.blocks.append(sshconfig.HostBlock(["Host dupe-test"]))
        check("duplicate alias blocks the save",
              rejects(cfg3.save, backup_dir=backup_dir))
        check("file untouched after a rejected save",
              open(path).read() == before)
    finally:
        os.unlink(path)

    print("\n== the starter template is correct by construction ==")
    tpl = tempfile.mktemp(suffix=".cfg")
    open(tpl, "w").write(sshconfig.TEMPLATE)
    t = SshConfig.load(tpl)
    check("template shadows nothing", t.shadowed_keys() == set(),
          str(sorted(t.shadowed_keys())))
    check("so the fix-up is a no-op on it", t.move_defaults_to_end() == 0)
    check("Host * sits at the end",
          sshconfig.TEMPLATE.index("Host *") > sshconfig.TEMPLATE.index("--- Servers ---"))
    check("template carries no username",
          "User " not in sshconfig.TEMPLATE.replace("#     User deploy", ""))
    os.unlink(tpl)

    print("\n== the fixture was never touched ==")
    check("fixture unchanged",
          open(FIXTURE, errors="replace").read() == original)

    print("\n" + ("ALL CHECKS PASSED" if not FAILURES else f"FAILURES: {FAILURES}"))
    return 1 if FAILURES else 0


def rejects(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except (ConfigError, ConflictError):
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
