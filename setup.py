"""Ship the same manifest catalog used by source checkouts in the wheel."""
from pathlib import Path
from shutil import copytree, ignore_patterns
from setuptools import setup
from setuptools.command.build_py import build_py


class Build(build_py):
    def run(self):
        super().run()
        target=Path(self.build_lib)/'agentlab_app/catalog'
        ignore = ignore_patterns('__pycache__', '*.pyc', '*.pyo')
        copytree('plugins/basicagent',target/'basicagent',dirs_exist_ok=True,ignore=ignore)
        for name in ('external','services'):
            copytree(Path('plugins/examples')/name,target/'examples'/name,dirs_exist_ok=True,ignore=ignore)


setup(cmdclass={'build_py':Build})
