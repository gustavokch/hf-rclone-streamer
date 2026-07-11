"""Setup configuration for HF to GDrive Streamer."""

from setuptools import setup, find_packages
from pathlib import Path

# Read README for long description
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text() if readme_file.exists() else ""

setup(
    name="hf-rclone-streamer",
    version="1.0.0",
    author="Your Name",
    author_email="your.email@example.com",
    description="Stream Hugging Face models to Google Drive via rclone",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/hf-rclone-streamer",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    python_requires=">=3.9",
    install_requires=[
        "huggingface-hub>=0.25.0",
        "rich>=13.7.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "black>=23.0.0",
            "mypy>=1.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "hf-rclone-streamer=hf_rclone_streamer:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
