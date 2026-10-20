"""Check final default-session export paths through the MCP protocol."""
import asyncio
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    server=StdioServerParameters(command=sys.executable,args=[str(ROOT/'veusz_mcp.py')],cwd=str(ROOT))
    async with stdio_client(server) as (r,w):
        async with ClientSession(r,w) as mcp:
            await mcp.initialize()
            async def call(name,**args):
                response=await mcp.call_tool(name,args)
                assert not response.isError,str(response.content)
                return response.structuredContent
            state=await call('veusz_status')
            if not state['datasets'] and not state['modified']:
                await call('veusz_open',path='output/v0.1/可编辑图形.vsz',document_id=state['document_id'],revision=state['revision'])
            state=await call('veusz_status')
            for ext in ('png','svg','pdf'):
                await call('veusz_export',path='output/v0.1/figure.'+ext,page=1,dpi=300,
                           document_id=state['document_id'],revision=state['revision'])
            await call('veusz_export',path='output/v0.1/transparent.png',page=1,dpi=100,background='#ffffff00',
                       document_id=state['document_id'],revision=state['revision'])
            preview=await mcp.call_tool('veusz_preview',{'document_id':state['document_id'],'revision':state['revision']})
            assert not preview.isError and any(c.type=='image' for c in preview.content)
            report_path=ROOT/'output/v0.1/verification.json'
            report=json.loads(report_path.read_text(encoding='utf-8'))
            report['manual_baseline']=state
            report['release']['live_window_pid']=state['pid']
            report['release']['final_export_mcp_probe']=True
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('PASS final MCP PNG/SVG/PDF, optional transparent background and image preview')


if __name__=='__main__':
    asyncio.run(main())
