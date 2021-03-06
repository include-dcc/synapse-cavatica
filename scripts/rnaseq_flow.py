"""
This is a a proof-of-concept flow for some RNA-seq data using this
KF RNA-Seq workflow: https://github.com/kids-first/kf-rnaseq-workflow
Link on CAVATICA: https://cavatica.sbgenomics.com/public/apps#cavatica/apps-publisher/kfdrc-rnaseq-workflow/

Here are the high level steps:

1. RNA-seq data files [FASTQ] and metadata [annotations/CSV] indexed in
   Synapse, stored in S3
2. Manual submission that couples synapseclient and CAVATICA API
3. Semi-automated submission using Synapse Evaluation API or AWS Lambda
4. Execution of processing workflow [CWL] in CAVATICA environment
    - if there should be status updates back to Synapse
      (e.g. 58% of processing is done)
4. Return of results [BAMs, TSVs] to Synapse, or elsewhere.

More details of this task can be found here:
https://github.com/include-dcc/stwg-issue-tracking/issues/7
"""
import json
import os
import tempfile

import sevenbridges as sbg
import synapseclient


def get_or_create_project(api, project_name):
   """Get or create a CAVATICA project

   Args:
      api: Synapse bridges connection
      project_name: Name of project

   Returns:
      CAVATICA Project

   """
   # pull out target project
   project = [p for p in api.projects.query(limit=100).all() \
              if p.name == project_name]

   if not project:
      print(f'Target project ({project_name}) not found, creating project')
      project = api.projects.create(name=project_name)
   else:
      project = project[0]
   return project


def copy_or_get_app(api, app_name, project):
   """Copy a public application if it doesnt exist in your project
   or get the existing public app

   Args:
      api: Seven bridges connection
      app_name: Name of public app
      project: CAVATICA project

   Returns:
      CAVATICA App
   """
   # Query all public application
   public_apps = api.apps.query(visibility='public').all()

   app = [public_app for public_app in public_apps
          if public_app.name == app_name][0]

   # Look for any duplicated application names in project
   project_apps = api.apps.query(project = project.id, limit=100)
   duplicate_app = [a for a in project_apps.all() if a.name == app.name]

   if duplicate_app:
      print('App already exists in second project, please try another app')
      copied_app = duplicate_app[0]
   else:
      # Copy the app if it doesn't exist
      print(f'App ({app_name}) does not exist in '
            f'Project ({project.name}); copying now')
      copied_app = app.copy(project = project.id, name = app_name)

      # re-list apps in target project to verify the copy worked
      my_apps = api.apps.query(project = project.id, limit=100)
      my_app_names = [a.name for a in my_apps.all()]

      if app_name in my_app_names:
         print('Sucessfully copied one app!')
      else:
         print('Something went wrong...')
   return copied_app


def read_json_submission(filepath):
   """Reads JSON submission"""
   try:
      with open(filepath, "r") as sub_f:
         submission_input = json.load(sub_f)
      return submission_input
   except Exception:
      # Can add validation of input parameters based on workflow here...
      raise ValueError("Input must be a valid json file")


def store_synid_to_cavatica(syn, sbg_api, input_json, cavatica_project_id):
   """Take any inputs that are synapse ids, download the entity,
   store them onto cavatica and update input json"""
   for key, value in input_json.items():
      if str(value).startswith("syn"):
         ent = syn.get(value)
         # Upload to CAVATICA
         sbg_api.files.upload(path=ent.path, project=cavatica_project_id)
         # Query for the cavatica file id
         files = sbg_api.files.query(project=cavatica_project_id)
         fastq_files = [
            cavatica_file for cavatica_file in files
            if cavatica_file.name == os.path.basename(ent.name)
         ]
         # Replace the synapse id with the cavatica file id
         input_json[key] = fastq_files[0]
   return input_json


def evaluate_submissions(syn, api):
   """Workflow to evaluate RECEIVED submissions"""
   # CAVATICA project name
   project_name = "Test"
   # Public CAVATIC app-rnaseq workflow
   app_name = "Kids First DRC RNAseq Workflow"
   project = get_or_create_project(api=api, project_name=project_name)

   # Copy an application to your CAVATICA project
   # https://github.com/sbg/okAPI/blob/d3bcdeca309534603ae715cf2646c5f65e89d98f/Recipes/CGC/apps_copyFromPublicApps.ipynb
   copied_app = copy_or_get_app(api=api, app_name=app_name, project=project)
   print(copied_app)

   # Get all submissions
   evaluation_queue_id = 9614883
   sub_bundles = syn.getSubmissionBundles(evaluation_queue_id,
                                          status='RECEIVED')
   for sub, sub_status in sub_bundles:
      sub_obj = syn.getSubmission(sub.id)

      workflow_input = read_json_submission(sub_obj.filePath)
      # TODO: this can use DRS ids.
      # Add step to download some fastq files from Synapse
      inputs = store_synid_to_cavatica(syn=syn, sbg_api=api,
                                      input_json=workflow_input,
                                      cavatica_project_id=project.id)

      # TODO: Missing rest call that automatically copies
      # reference files?
      # Name the cavatica task the submission id
      task = api.tasks.create(name=sub.id, project=project.id,
                              app=copied_app.id, inputs=inputs, run=True)
      task_id = task.id
      # Create task_id output folder to potentially store task output
      # files into synapse
      synapse_project = "syn25920979"
      synapse_task_folder = syn.store(
         synapseclient.Folder(name=task_id, parent=synapse_project)
      )
      # Create dict for submission annotations
      if not sub_status.get("submissionAnnotations"):
         sub_status.submissionAnnotations = {}

      # Update submission status to evaluation in progress
      sub_status.submissionAnnotations["task_id"] = task_id
      sub_status.submissionAnnotations["task_output"] = synapse_task_folder.id
      sub_status.status = "EVALUATION_IN_PROGRESS"
      sub_status = syn.store(sub_status)


def monitor_submissions(syn, api):
   """Monitoring evaluating submissions"""
   evaluation_queue_id = 9614883
   sub_bundles = syn.getSubmissionBundles(evaluation_queue_id,
                                          status='EVALUATION_IN_PROGRESS')
   for _, sub_status in sub_bundles:
      task_id = sub_status.submissionAnnotations['task_id'][0]
      synapse_task_folder = sub_status.submissionAnnotations['task_output'][0]
      task = api.tasks.get(task_id)
      # Update final submission status
      if task.status == "INVALID":
         sub_status.status = "INVALID"
      elif task.status == "QUEUED":
         print("Task is queued")
         continue
      elif task.status == "RUNNING":
         execution_details = task.get_execution_details()
         jobs_completed = [
            job.status == "COMPLETED" for job in execution_details.jobs
         ]
         print(
            "Task is running. "
            f"{sum(jobs_completed)}/{len(jobs_completed)} jobs completed."
         )
         continue
      elif task.status == "COMPLETED":
         sub_status.status = "ACCEPTED"
         # Store task outputs if the task is complete
         task_outputs = task.outputs
         # Download outputs except for bam files (large)
         temp_dir = tempfile.TemporaryDirectory()
         output_files = []
         for _, output_value in task_outputs.items():
            if output_value is not None:
               if not output_value.name.endswith(".bam"):
                  output_file = api.files.get(output_value.id)
                  output_path = os.path.join(temp_dir.name, output_value.name)
                  output_file.download(output_path)
                  output_files.append(output_path)

         # Unfortunately no recursive store function currently...
         for output_file in output_files:
            syn.store(
               synapseclient.File(path=output_file, parent=synapse_task_folder)
            )
         temp_dir.cleanup()
      else:
         raise ValueError(f"status {task.status} not supported:")
      sub_status = syn.store(sub_status)


def main():
   """Invoke"""
   # Setup Seven bridges API
   # https://github.com/sbg/okAPI/blob/a6c0816235ae8742913950d38cc5f57b5ab6314e/Recipes/CGC/Setup_API_environment.ipynb
   # Pull credential from ~/.sevenbridges/credentials
   config_file = sbg.Config(profile='cavatica')
   api = sbg.Api(config=config_file)

   # Login to synapse
   syn = synapseclient.login()

   # Evaluate and monitor submissions
   evaluate_submissions(syn, api)
   monitor_submissions(syn, api)


if __name__ == "__main__":
   main()
