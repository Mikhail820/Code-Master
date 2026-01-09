from setuptools import setup, find_packages

setup(
    name="codemaster",
    version="1.0.0",
    description="SaaS-платформа для создания ботов-визиток в Telegram",
    author="CodeMaster Team",
    packages=find_packages(),
    install_requires=[
        "aiogram==3.3.0",
        "aiosqlite==0.19.0",
        "python-dotenv==1.0.0",
        "cryptography==41.0.7",
        "aiohttp==3.9.1",
        "pydantic==2.5.0",
    ],
    python_requires=">=3.10",
)
