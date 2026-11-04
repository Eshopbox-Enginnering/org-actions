"""Check the shared setup on AJIO revisions predating its test fixtures."""

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/java-test-coverage.yml'


class AjioCoverageSetupTests(unittest.TestCase):
    def test_old_pr_without_fixture_gets_eager_isolated_setup_on_both_revisions(self):
        step = WORKFLOW.read_text().split('      - name: Prepare isolated AJIO test configuration\n', 1)[1]
        body = step.split("          python3 <<'PYTHON'\n", 1)[1]
        script = textwrap.dedent(body.split('\n          PYTHON', 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for revision in ('base', 'pr'):
                test = root / revision / 'src/test/java/com/ajio/EsbAjioIntegrationApplicationTests.java'
                test.parent.mkdir(parents=True)
                test.write_text('existing test stays unchanged')
                production = root / revision / 'src/main/resources/application.properties'
                production.parent.mkdir(parents=True)
                production.write_text('production stays unchanged')
            result = subprocess.run(
                ['python3', '-c', script], cwd=root,
                env={**os.environ, 'MYSQL_PORT': '15432', 'REDIS_PORT': '16380'},
                capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            base = root / 'base'
            pr = root / 'pr'
            resources = Path('src/test/resources')
            properties = (base / resources / 'application.properties').read_text()
            self.assertIn('jdbc:mysql://127.0.0.1:15432/ajio_local', properties)
            self.assertIn('spring.data.redis.port=16380', properties)
            self.assertNotIn('spring.main.lazy-initialization=true', properties)
            self.assertEqual(properties, (pr / resources / 'application.properties').read_text())
            listener = Path('src/test/java/com/ajio/support/CloudTasksTestExecutionListener.java')
            self.assertEqual((base / listener).read_text(), (pr / listener).read_text())
            self.assertIn('CloudTasksTestExecutionListener',
                          (base / resources / 'META-INF/spring.factories').read_text())
            self.assertEqual('mock-maker-inline\n',
                             (base / resources / 'mockito-extensions/org.mockito.plugins.MockMaker').read_text())
            for revision in ('base', 'pr'):
                self.assertEqual('existing test stays unchanged',
                                 (root / revision / 'src/test/java/com/ajio/EsbAjioIntegrationApplicationTests.java').read_text())
                self.assertEqual('production stays unchanged',
                                 (root / revision / 'src/main/resources/application.properties').read_text())


if __name__ == '__main__':
    unittest.main()
