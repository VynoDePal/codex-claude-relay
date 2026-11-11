import unittest,json
from claude_relay import prepare,convert,metadata
class Tests(unittest.TestCase):
 def payload(self):return {'model':'claude-code-opus','input':[{'role':'user','content':'Hello'}],'tools':[{'type':'namespace','name':'functions','tools':[{'type':'function','name':'read','parameters':{'type':'object'}}]},{'type':'custom','name':'patch','format':{'type':'text'}}]}
 def test_tool_names_and_outputs_roundtrip(self):
  p=self.payload();chat,mapping=prepare(p);alias=next(n for n,v in mapping.items() if v['name']=='read')
  out=convert({'structured_output':{'text':'','calls':[{'name':alias,'arguments':'{"path":"x"}'}]},'usage':{},'modelUsage':{'claude-sonnet-5':{}}},p,mapping)
  c=out['output'][0];self.assertEqual(c['name'],'read');self.assertEqual(c['namespace'],'functions');self.assertEqual(c['type'],'function_call')
  p['input'] += [c,{'type':'function_call_output','call_id':c['call_id'],'output':'FILE CONTENT'}]
  chat,_=prepare(p);self.assertEqual(chat['messages'][-1]['role'],'tool');self.assertIn('FILE CONTENT',chat['messages'][-1]['content'])
 def test_custom_tool_input_preserved(self):
  p=self.payload();_,m=prepare(p)
  r=convert({'structured_output':{'text':'','calls':[{'name':'patch','arguments':json.dumps({'input':'*** Begin Patch\n*** End Patch'})}]}},p,m)
  self.assertEqual(r['output'][0]['type'],'custom_tool_call');self.assertTrue(r['output'][0]['input'].startswith('*** Begin Patch'))
 def test_unknown_tool_rejected(self):
  p=self.payload();_,m=prepare(p)
  with self.assertRaises(ValueError):convert({'structured_output':{'text':'','calls':[{'name':'invented','arguments':'{}'}]}},p,m)
 def test_bad_arguments_rejected(self):
  p=self.payload();_,m=prepare(p)
  with self.assertRaises(ValueError):convert({'structured_output':{'text':'','calls':[{'name':next(iter(m)),'arguments':'broken json'}]}},p,m)
 def test_no_tools_choice_enforced(self):
  p=self.payload();p['tool_choice']='none';_,m=prepare(p)
  with self.assertRaises(ValueError):convert({'structured_output':{'text':'','calls':[{'name':next(iter(m)),'arguments':'{}'}]}},p,m)
 def test_text_and_usage(self):
  p=self.payload();_,m=prepare(p)
  r=convert({'structured_output':{'text':'Bonjour','calls':[]},'usage':{'input_tokens':2,'cache_read_input_tokens':3,'cache_creation_input_tokens':4,'output_tokens':5},'modelUsage':{'claude-sonnet-5':{}}},p,m)
  self.assertEqual(r['usage']['input_tokens'],9);self.assertEqual(r['output'][0]['phase'],'final_answer')
  self.assertIn('claude-sonnet-5',r['metadata']['claude_models_used'])
 def test_catalog_all_visible(self):
  self.assertEqual([m['slug'] for m in metadata()],['claude-code-opus','claude-code-fable','claude-code-sonnet']);self.assertTrue(all(m['visibility']=='list' for m in metadata()))
 def test_sonnet_profile(self):
  sonnet=next(m for m in metadata() if m['slug']=='claude-code-sonnet')
  self.assertEqual(sonnet['display_name'],'Claude Sonnet');self.assertEqual(sonnet['default_reasoning_level'],'medium')
  self.assertEqual([l['effort'] for l in sonnet['supported_reasoning_levels']],['low','medium','high','xhigh','max'])
  self.assertIn('Claude Sonnet 5.5',sonnet['description'])
 def test_fable_profile(self):
  fable=next(m for m in metadata() if m['slug']=='claude-code-fable')
  self.assertEqual(fable['display_name'],'Claude Fable');self.assertEqual(fable['default_reasoning_level'],'medium')
  self.assertEqual([l['effort'] for l in fable['supported_reasoning_levels']],['low','medium','high','xhigh','max'])
  self.assertIn('Claude Fable 5.2',fable['description'])
if __name__=='__main__':unittest.main()
