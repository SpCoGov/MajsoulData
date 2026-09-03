import re
import unittest
from pathlib import Path


class RepositoryConfigTests(unittest.TestCase):
    def test_python_requirements_are_exactly_pinned(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8").splitlines()
        packages = [line for line in requirements if line and not line.startswith("#")]

        self.assertGreaterEqual(len(packages), 2)
        for package in packages:
            self.assertRegex(package, r"^[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+$")

    def test_workflow_pins_protoc_gen_doc_and_validates_generated_outputs(self):
        workflow = Path(".github/workflows/update.yml").read_text(encoding="utf-8")

        self.assertIn("go-version: '1.23.4'", workflow)
        self.assertNotIn("go-version: 'stable'", workflow)
        self.assertIn("github.com/pseudomuto/protoc-gen-doc/cmd/protoc-gen-doc@v", workflow)
        self.assertNotIn("protoc-gen-doc@latest", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("python -m compileall -q main.py src tests", workflow)
        self.assertIn("json ok", workflow)
        self.assertIn("protoc --proto_path=data/protocol/proto", workflow)
        self.assertIn("python main.py --asset-mode all", workflow)
        self.assertIn("repository: SpCoGov/MajsoulAssets", workflow)
        self.assertIn("--asset-dir asset-repo/assets", workflow)
        self.assertIn('Path("asset-repo/assets/manifest.json")', workflow)
        self.assertIn("git add -A data/ raw/", workflow)
        self.assertIn("git add -A assets/", workflow)
        self.assertIn("git lfs install --local", workflow)

    def test_gitattributes_hides_raw_diff_and_marks_generated_outputs(self):
        attributes = Path(".gitattributes").read_text(encoding="utf-8")

        self.assertRegex(attributes, re.compile(r"^raw/\*\*.*linguist-generated=true.*-diff", re.M))
        self.assertNotIn("assets/**", attributes)
        self.assertRegex(
            attributes,
            re.compile(r"^data/protocol/protocol\.md.*linguist-generated=true", re.M),
        )
        self.assertRegex(
            attributes,
            re.compile(r"^data/protocol/schema\.json.*linguist-generated=true", re.M),
        )


if __name__ == "__main__":
    unittest.main()
