import os

from jarvis.core.child_process import clean_external_path


def test_external_runtime_path_excludes_only_gui_bundle() -> None:
    root = os.path.abspath("gui_bundle")
    sibling = root + "_other"
    value = os.pathsep.join(
        [root, os.path.join(root, "torch", "lib"), sibling, os.path.abspath("engine")]
    )
    assert clean_external_path(value, root).split(os.pathsep) == [
        sibling,
        os.path.abspath("engine"),
    ]
