import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpu_agents import Store, build_command, parse_events, DEFAULTS, validate_settings


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.store = Store(self.base / 'state')
        self.team = self.store.create_team({'project_path':str(self.project),'max_agents':2})

    def test_limits_and_model_allowlist_cannot_be_overridden_by_manager(self):
        with self.assertRaisesRegex(ValueError,'max_agents'):
            self.store.create_team({'project_path':str(self.project),'max_agents':33})
        with self.assertRaisesRegex(ValueError,'disabled'):
            self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Read files','model':'deepseek','role':'reviewer'})
        with self.assertRaisesRegex(ValueError,'model'):
            self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Read','model':'gpt-6.1-sol','role':'reviewer'})
        for _ in range(2):
            task = self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Read files','role':'reviewer'})
            self.store.finish(task['task_id'],task['turn_id'],{'status':'completed','reply':'Read.'})
        with self.assertRaisesRegex(ValueError,'agent limit'):
            self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Read','role':'reviewer'})

    def test_concurrency_cap_applies_across_teams(self):
        for _ in range(2):
            self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Read','role':'reviewer'})
        another = self.store.create_team({'project_path':str(self.project)})
        with self.assertRaisesRegex(ValueError,'concurrency'):
            self.store.reserve_task({'team_id':another['team_id'],'prompt':'Read','role':'reviewer'})

    def test_writers_are_exclusive_across_overlapping_and_symlinked_paths(self):
        task=self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Implement fix'})
        nested=self.project/'nested';nested.mkdir()
        alias=self.base/'alias';alias.symlink_to(nested,target_is_directory=True)
        second=self.store.create_team({'project_path':str(alias)})
        with self.assertRaisesRegex(ValueError,'overlap'):
            self.store.reserve_task({'team_id':second['team_id'],'prompt':'Review','role':'reviewer'})
        self.store.finish(task['task_id'],task['turn_id'],{'status':'completed','reply':'Fixed'})
        reviewer=self.store.reserve_task({'team_id':second['team_id'],'prompt':'Review','role':'reviewer'})
        with self.assertRaisesRegex(ValueError,'overlap'):
            self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Change code'})

    def test_continuation_reuses_slot_and_ignores_stale_results(self):
        task=self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Implement feature'})
        with self.assertRaisesRegex(ValueError,'running'):
            self.store.reserve_continue({'task_id':task['task_id'],'prompt':'Feedback'})
        self.store.finish(task['task_id'],task['turn_id'],{'status':'completed','reply':'First result','logs':{'events':'previous-turn.jsonl'}})
        resumed=self.store.reserve_continue({'task_id':task['task_id'],'prompt':'Fix failing test'})
        self.assertEqual(resumed['task_id'],task['task_id'])
        self.assertNotEqual(resumed['turn_id'],task['turn_id'])
        self.assertEqual(resumed['agent_number'],1)
        self.assertNotIn('logs',resumed)
        self.assertIn('First result',resumed['prompt'])
        self.assertIn('Fix failing test',resumed['prompt'])
        self.store.finish(task['task_id'],task['turn_id'],{'status':'completed','reply':'STALE'})
        self.assertEqual(self.store.read_task(task['task_id'])['turn_id'],resumed['turn_id'])

    def test_disabled_model_revocation_applies_to_existing_team_and_resume(self):
        settings={**DEFAULTS,'enabled_models':['glm-5.3-flash','deepseek-v4.1']}
        self.store.configure(settings)
        team=self.store.create_team({'project_path':str(self.project)})
        task=self.store.reserve_task({'team_id':team['team_id'],'prompt':'Read','role':'reviewer','model':'deepseek'})
        self.store.finish(task['task_id'],task['turn_id'],{'status':'completed','reply':'Read'})
        self.store.configure(DEFAULTS)
        with self.assertRaisesRegex(ValueError,'disabled'):
            self.store.reserve_continue({'task_id':task['task_id'],'prompt':'Read again'})

    def test_invalid_paths_ids_and_prompts(self):
        for path in ['relative/path','/',str(Path.home()),str(self.base/'missing')]:
            with self.assertRaises(ValueError): self.store.create_team({'project_path':path})
        for identifier in ['../config','anything','']:
            with self.assertRaises(ValueError): self.store.read_task(identifier)
        for prompt in ['',None,' ']:
            with self.assertRaises(ValueError): self.store.reserve_task({'team_id':self.team['team_id'],'prompt':prompt})
        self.assertEqual(self.store.list_tasks()['tasks'],[])

    def test_project_cannot_include_the_private_state_directory(self):
        nested_store=Store(self.project/'private-state')
        with self.assertRaises(ValueError):nested_store.create_team({'project_path':str(self.project)})
        with self.assertRaises(ValueError):self.store.create_team({'project_path':str(self.base)})

    def test_cancel_retains_ownership_until_process_shutdown_finishes(self):
        task=self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Write'})
        def during_shutdown(*args,**kwargs):
            with self.assertRaisesRegex(ValueError,'overlap'):
                self.store.reserve_task({'team_id':self.team['team_id'],'prompt':'Write again'})
            with self.assertRaisesRegex(ValueError,'running'):
                self.store.reserve_continue({'task_id':task['task_id'],'prompt':'Continue'})
        with patch('gpu_agents.stop_process',side_effect=during_shutdown):
            self.store.cancel({'task_id':task['task_id']})
        self.assertEqual(self.store.read_task(task['task_id'])['status'],'cancelled')

    def test_settings_are_validated_before_atomic_write(self):
        before=self.store.settings()
        for patch in [{'max_agents':True},{'concurrency':0},{'timeout_seconds':1},{'enabled_models':['unknown']},{'oops':1}]:
            with self.assertRaises(ValueError):self.store.configure({**before,**patch})
        self.assertEqual(self.store.settings(),before)
        self.assertEqual((self.store.root/'config.json').stat().st_mode & 0o777,0o600)


class ExecutionTests(unittest.TestCase):
    def test_only_explicit_model_and_restricted_tools_are_used(self):
        for model in ('glm-5.3-flash','deepseek-v4.1'):
            for role,sandbox in [('implementer','workspace-write'),('tester','workspace-write'),('reviewer','read-only')]:
                task={'model':model,'role':role,'project_path':'/tmp/fixture','reasoning_effort':'high'}
                cmd=build_command(task,Path('/tmp/private-output.txt'))
                self.assertEqual(cmd[cmd.index('-m')+1],model)
                self.assertEqual(cmd[cmd.index('-s')+1],sandbox)
                for setting in ['agents.enabled=false','features.apps=false','features.plugins=false']:
                    self.assertIn(setting,cmd)
                self.assertIn('--ignore-user-config',cmd)
                self.assertIn('--ephemeral',cmd)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',cmd)
                self.assertEqual(cmd[cmd.index('-a')+1],'never')
                self.assertEqual(cmd[-1],'-')

    def test_final_message_is_not_proof_of_tests_or_success_without_terminal_event(self):
        events=[{'type':'item.completed','item':{'type':'agent_message','text':'All tests pass'}}]
        parsed=parse_events(events,0,'All tests pass')
        self.assertEqual(parsed['status'],'failed')
        events.append({'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':5}})
        parsed=parse_events(events,0,'All tests pass')
        self.assertEqual(parsed['status'],'completed')
        self.assertEqual(parsed['commands'],[])
        self.assertEqual(parse_events(events,1,'All tests pass')['status'],'failed')

    def test_real_command_outcomes_and_provider_failures_are_retained(self):
        events=[{'type':'item.completed','item':{'type':'command_execution','command':'python3 -m unittest','exit_code':1,'aggregated_output':'FAILED (failures=1)'}},
                {'type':'turn.failed','error':{'message':'GPU unavailable'}}]
        result=parse_events(events,1,'')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['commands'][0]['exit_code'],1)
        self.assertIn('GPU unavailable',result['error'])


if __name__=='__main__':unittest.main()
