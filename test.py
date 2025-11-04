print("🚀 Starting balance test...")

import asyncio
from balance_check import fetch_card_balance

async def main():
    print("🧠 Checking card balance...")
    bal = await fetch_card_balance("435880", "4358802041855353", "12/30", "338")
    print("Balance:", bal)

if __name__ == "__main__":
    asyncio.run(main())
