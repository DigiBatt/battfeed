from setuptools import setup, find_packages

setup(
    name="gleaned",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "pandas",
        "wmi",
        "pyarrow",
    ],
    extras_require={
        "dev": [
            "rdflib",
            "pydantic",
            "pydantic-schemaorg",
            "requests",
            "jinja2",
            "owlrl",
            "EMMOntoPy"
            ],  # Development dependencies
    },
    entry_points={},
    python_requires=">=3.7",
    description="A Python package for harvesting battery data.",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/DigiBatt/gleaned",
    author="Simon Clark",
    author_email="your-email@example.com",
)
