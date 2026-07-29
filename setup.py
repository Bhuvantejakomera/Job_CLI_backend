from setuptools import find_packages, setup


setup(
    name="queuectl",
    version="0.1.0",
    description="CLI background job queue for the QueueCTL backend assignment",
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "queuectl=queuectl.cli:main",
        ]
    },
)

