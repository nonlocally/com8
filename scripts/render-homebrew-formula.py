#!/usr/bin/env python3
"""Generate a formula for an immutable, checksum-verified COM8 release archive."""
import argparse
import re
from pathlib import Path

TEMPLATE = '''class Com8 < Formula
  desc "Persistent agent identities, messaging, and execution"
  homepage "https://github.com/nonlocally/com8"
  url "https://github.com/nonlocally/com8/releases/download/v@VERSION@/com8-@VERSION@.tar.gz"
  sha256 "@SHA256@"
  license "MIT"

  depends_on "bash"
  depends_on "node"
  depends_on "python@3.14"

  # Every runtime file is checksummed; wrappers provide the dependency PATH.
  # Homebrew's shebang rewrite would invalidate the installed archive.
  skip_clean "libexec"

  def skip_clean?(path)
    # Reinstall can expose prefix through opt while Cleaner walks its real
    # Cellar path. Protect either spelling of the immutable runtime directory.
    return true if path == libexec || (libexec.directory? && path == libexec.realpath)

    super
  end

  def install
    libexec.install Dir["*"]
    # Homebrew moves metafiles out of libexec when the prefix has none.
    # Expose the license at the prefix while retaining the immutable runtime.
    prefix.install_symlink libexec/"LICENSE"
    (bin/"com8").write_env_script libexec/"bin/com8",
      PATH: "#{Formula["node"].opt_bin}:#{Formula["python@3.14"].opt_bin}:#{Formula["bash"].opt_bin}:$PATH"
    (bin/"communicate").write_env_script libexec/"bin/communicate",
      PATH: "#{Formula["node"].opt_bin}:#{Formula["python@3.14"].opt_bin}:#{Formula["bash"].opt_bin}:$PATH"
  end

  def caveats
    <<~EOS
      Choose your clients and optional workstation tools:
        com8 setup
        com8 doctor

      Preview an explicit selection, including missing dependencies:
        com8 setup --install-missing --claude --codex --terminal --mesh --dry-run
      Replace --dry-run with --yes to apply it. Add --ghostty on macOS if wanted.

      To enable the persistent local daemon:
        com8 setup --service

      Terminal and mesh profiles are optional. Preview before applying:
        com8 profile preview --terminal --mesh

      Guided setup offers missing selected tools and clients. Login is a separate choice.
      Selected clients include tmux for agent seats; terminal configuration remains optional.
      Terminal shortcuts work from zsh or Bash; your interactive and login shells stay unchanged.
      Existing clients are not implicitly upgraded; COM8 uninstall keeps third-party packages.
      Installing or upgrading this formula does not replace your terminal configuration.
    EOS
  end

  test do
    require "json"
    require "digest"
    JSON.parse((libexec/"release.json").read).fetch("files").each do |name, sha|
      assert_equal sha, Digest::SHA256.file(libexec/name).hexdigest
    end
    assert_match "@VERSION@", shell_output("#{bin}/com8 version")
    assert_match "bus", shell_output("#{bin}/com8 --help")
    ENV["HOME"] = testpath
    ENV["COMMUNICATE_DATA"] = testpath/"data"
    ENV["COMM_STATE"] = testpath/"state"
    system bin/"com8", "setup", "--no-clients", "--dry-run"
    refute_path_exists testpath/"data"
  end
end
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("version")
    p.add_argument("sha256")
    p.add_argument("output", type=Path)
    args = p.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", args.version):
        p.error("invalid release version")
    if not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
        p.error("invalid SHA-256")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(TEMPLATE.replace("@VERSION@", args.version).replace("@SHA256@", args.sha256))


if __name__ == "__main__":
    main()
