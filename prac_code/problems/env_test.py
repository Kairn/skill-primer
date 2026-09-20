#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///

"""
This is a simple environment test script to verify that the Python environment is set up correctly.
"""

import asyncio
import random


async def main():
    print("Starting the environment test...")
    await asyncio.sleep(1)  # Simulate some async work
    random_number = random.randint(1000, 9999)
    print(f"Env test completed. Have a lucky number for the day: {random_number}!")


if __name__ == "__main__":
    asyncio.run(main())
