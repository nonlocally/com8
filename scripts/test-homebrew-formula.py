#!/usr/bin/env python3
"""Check rendered cleanup policy without installing Homebrew or changing a keg.

The small Ruby base models Homebrew's lexical skip_clean? contract. Its cleaner
walks prefix.realpath; after an opt link exists these paths have different roots.
The disposable CI qualifier separately exercises the actual Homebrew lifecycle.
"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
RUBY = r'''
require "pathname"
require "tmpdir"
require "fileutils"
require "find"

class Formula
  class << self
    attr_accessor :skip_paths
    def inherited(child); child.skip_paths = []; end
    def skip_clean(*paths); skip_paths.concat(paths); end
    def desc(*); end
    def homepage(*); end
    def url(*); end
    def sha256(*); end
    def license(*); end
    def depends_on(*); end
    def test; end
  end
  attr_reader :prefix
  def initialize(prefix); @prefix = prefix; end
  def libexec; prefix/"libexec"; end
  # Homebrew Formula#skip_clean? compares relative paths lexically.
  def skip_clean?(path)
    self.class.skip_paths.include?(path.relative_path_from(prefix).to_s)
  end
end

load ARGV.fetch(0)
def require_check(value, message); raise message unless value; end

Dir.mktmpdir("com8-formula-") do |root|
  root = Pathname(root).realpath
  keg = root/"Cellar/com8/0.3.0"
  opt = root/"opt/com8"
  (keg/"libexec/node_modules").mkpath
  (keg/"bin").mkpath
  opt.parent.mkpath
  opt.make_symlink(keg)
  Com8.skip_clean "share/fixture"
  [keg, opt].each do |prefix|
    formula = Com8.new(prefix)
    require_check(formula.skip_clean?(prefix/"libexec"), "lexical libexec not protected")
    require_check(formula.skip_clean?(keg/"libexec"), "resolved libexec not protected through opt prefix")
    require_check(!formula.skip_clean?(keg/"bin"), "cleanup outside libexec was disabled")
    require_check(!formula.skip_clean?(keg/"libexec-other"), "unrelated sibling was protected")
    require_check(formula.skip_clean?(prefix/"share/fixture"), "default cleanup policy was not delegated")
    payload = keg/"libexec/node_modules/example.d.ts"
    wrapper = keg/"bin/example"
    original = "#!/usr/bin/env node\n// immutable payload\n"
    payload.write(original)
    wrapper.write(original)
    # Cleaner#rewrite_shebangs uses prefix.realpath and prunes skip_clean dirs.
    prefix.realpath.find do |path|
      Find.prune if formula.skip_clean?(path)
      next if path.directory? || path.symlink?
      path.write(path.read.sub("#!/usr/bin/env node", "#!/fixture/node"))
    end
    require_check(payload.read == original, "cleanup rewrote an immutable payload shebang")
    require_check(wrapper.read.start_with?("#!/fixture/node"), "ordinary wrapper cleanup was skipped")
  end
end
puts "PASS: rendered formula protects fresh/opt-linked runtime and retains cleanup elsewhere"
'''


def main():
    ruby = shutil.which("ruby")
    if not ruby:
        raise SystemExit("Ruby is required for the formula cleanup regression")
    spec = importlib.util.spec_from_file_location("com8_formula", ROOT / "render-homebrew-formula.py")
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    with tempfile.TemporaryDirectory(prefix="com8-formula-test-") as temporary:
        formula = Path(temporary) / "com8.rb"
        formula.write_text(renderer.TEMPLATE.replace("@VERSION@", "0.3.0").replace("@SHA256@", "0" * 64))
        subprocess.run([ruby, "-", str(formula)], input=RUBY, text=True, check=True)


if __name__ == "__main__":
    main()
