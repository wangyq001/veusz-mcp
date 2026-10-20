"""Read-only STDIO connection probe for the default delivered session."""
import asyncio
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    server=StdioServerParameters(command=sys.executable,args=[str(ROOT/'veusz_mcp.py')],cwd=str(ROOT))
    async with stdio_client(server) as (r,w):
        async with ClientSession(r,w) as mcp:
            await mcp.initialize()
            tools=await mcp.list_tools()
            status=await mcp.call_tool('veusz_status')
            assert not status.isError,str(status.content)
            state=status.structuredContent
            assert state['bridge_version']=='0.1.0'
            print(json.dumps({'tools':len(tools.tools),'pid':state['pid'],
                'filename':state['filename'],'revision':state['revision'],'modal':state['modal']},
                ensure_ascii=False,indent=2))


if __name__=='__main__':
    asyncio.run(main())
