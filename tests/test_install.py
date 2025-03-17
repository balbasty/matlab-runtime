from matlab_runtime_installer import install


def test_install_r2024b():
    install("R2024b", auto_answer=True)


def test_install_r2024a():
    install("R2024a", auto_answer=True)
