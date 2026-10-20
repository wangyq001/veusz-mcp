"""Human edit preservation, exercised through the actual MCP SDK."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    report_path=ROOT/'output/v0.1/verification.json'
    report=json.loads(report_path.read_text(encoding='utf-8'))
    env=dict(os.environ,PYTHONIOENCODING='utf-8')
    config=StdioServerParameters(command=sys.executable,args=[str(ROOT/'veusz_mcp.py')],env=env,cwd=str(ROOT))
    async with stdio_client(config) as (r,w):
        async with ClientSession(r,w) as mcp:
            await mcp.initialize()
            async def call(name,**args):
                response=await mcp.call_tool(name,args)
                if response.isError:
                    raise RuntimeError(str(response.content))
                return response.structuredContent
            async def write(name,**args):
                s=await call('veusz_status')
                return await call(name,document_id=s['document_id'],revision=s['revision'],**args)
            key='/acceptance/graph/measurement/key'
            line='/acceptance/graph/measurement/PlotLine/'
            state=await call('veusz_status')
            before=await call('veusz_inspect',paths=[key,line+'color',line+'width'])
            assert before['values'][key]=='v1-manual','Waiting for the human Key text edit'
            baseline=report['manual_baseline']
            assert state['document_id']==baseline['document_id']
            assert any(e['source']=='gui' and e['revision']>baseline['revision'] for e in state['events'])
            stale=await mcp.call_tool('veusz_apply_changes',{'document_id':baseline['document_id'],
                'revision':baseline['revision'],'changes':[{'path':key,'value':'must-not-overwrite'}]})
            assert stale.isError and 'REVISION_CONFLICT' in str(stale.content)
            await write('veusz_apply_changes',changes=[{'path':line+'color','value':'#c026d3'},
                                                       {'path':line+'width','value':'2.5pt'}])
            after=await call('veusz_inspect',paths=[key,line+'color',line+'width'])
            assert after['values'][key]=='v1-manual'
            await write('veusz_undo')
            undone=await call('veusz_inspect',paths=[key,line+'color',line+'width'])
            assert undone['values']==before['values']
            await write('veusz_apply_changes',changes=[{'path':line+'color','value':'#c026d3'},
                                                       {'path':line+'width','value':'2.5pt'}])
            await write('veusz_save',path='output/v0.1/用户精修.vsz',overwrite=True)
            for extension in ('png','pdf','svg'):
                await write('veusz_export',path='output/v0.1/用户精修.'+extension,page=1,dpi=300)
            report['human_edit']={'passed':True,'native_gui_event':True,'stale_write_rejected':True,
                'subsequent_edit_preserved_human_text':True,'undo_preserved_human_text':True,
                'saved_native_document':True,'before':before,'after':after}
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('PASS actual MCP preserved human text, rejected stale write, undid only program edits, and saved the native document')


if __name__=='__main__':
    asyncio.run(main())
