import os

from setuptools import find_packages, setup

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "README.md"), "r", encoding="utf-8") as handle:
    LONG_DESCRIPTION = handle.read()

setup(
    name="trainmon",
    version="0.1.0",
    description="Lightweight, zero-dependency training monitor with ETA and alerts.",
    long_description=LONG_DESCRIPTION,
    long_description_content_type="text/markdown",
    author="Wangye Fu",
    author_email="Wangye-Fu@users.noreply.github.com",
    url="https://github.com/Wangye-Fu/trainmon",
    project_urls={
        "Source": "https://github.com/Wangye-Fu/trainmon",
        "Issues": "https://github.com/Wangye-Fu/trainmon/issues",
    },
    license="MIT",
    packages=find_packages(exclude=("tests", "tests.*")),
    package_data={"trainmon": ["web/*.html", "web/*.js", "web/*.css"]},
    include_package_data=True,
    zip_safe=False,
    python_requires=">=3.6",
    install_requires=[],
    keywords="training monitor, machine learning, pytorch, eta, dashboard",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: System :: Monitoring",
    ],
    entry_points={
        "console_scripts": [
            "trainmon=trainmon.cli:main",
        ],
    },
)
