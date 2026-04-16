locals {
  runner_command = templatefile("${path.module}/templates/runner_command.sh.tftpl", {
    artifacts_bucket                 = aws_s3_bucket.artifacts.bucket
    duckdb_memory_limit              = var.duckdb_memory_limit
    duckdb_threads                   = var.duckdb_threads
    live_bucket                      = aws_s3_bucket.live.bucket
    parallel_chunk_insert_lines      = var.parallel_chunk_insert_lines
    parallel_target_chunk_multiplier = var.parallel_target_chunk_multiplier
    parallel_workers                 = var.parallel_workers
    decompress_workers               = var.decompress_workers
    web_batch_size                   = var.web_batch_size
  })
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.resource_prefix}-pipeline"
  role_arn = aws_iam_role.step_functions.arn

  logging_configuration {
    include_execution_data = true
    level                  = "ALL"

    log_destination = "${aws_cloudwatch_log_group.step_functions.arn}:*"
  }

  definition = jsonencode({
    Comment        = "Redlink Database pipeline"
    TimeoutSeconds = var.state_machine_timeout_seconds
    StartAt        = "LaunchRunner"
    States = {
      LaunchRunner = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ec2:runInstances"
        Parameters = {
          LaunchTemplate = {
            LaunchTemplateId = aws_launch_template.runner.id
            Version          = "$Default"
          }
          MinCount = 1
          MaxCount = 1
          TagSpecifications = [
            {
              ResourceType = "instance"
              Tags = [
                {
                  Key   = "Name"
                  Value = "${var.resource_prefix}-runner"
                },
                {
                  Key       = "DumpVersion"
                  "Value.$" = "$.dump_version"
                },
                {
                  Key       = "Wiki"
                  "Value.$" = "$.wiki_name"
                }
              ]
            }
          ]
        }
        ResultPath = "$.ec2"
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 5
            MaxAttempts     = 3
            BackoffRate     = 2
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "NotifyLaunchFailure"
          }
        ]
        Next = "CaptureInstanceId"
      }
      CaptureInstanceId = {
        Type = "Pass"
        Parameters = {
          "dump_version.$"    = "$.dump_version"
          "language.$"        = "$.language"
          "wiki_name.$"       = "$.wiki_name"
          "force.$"           = "$.force"
          "execution_id.$"    = "$$.Execution.Id"
          "execution_start.$" = "$$.Execution.StartTime"
          "instance_id.$"     = "$.ec2.Instances[0].InstanceId"
        }
        ResultPath = "$"
        Next       = "PrepareRunnerCommand"
      }
      PrepareRunnerCommand = {
        Type = "Pass"
        Parameters = {
          "dump_version.$"                = "$.dump_version"
          "language.$"                    = "$.language"
          "wiki_name.$"                   = "$.wiki_name"
          "force.$"                       = "$.force"
          "execution_id.$"                = "$.execution_id"
          "execution_start.$"             = "$.execution_start"
          "instance_id.$"                 = "$.instance_id"
          "command_export_language.$"     = "States.Format('export REDLINK_LANGUAGE={}', $.language)"
          "command_export_dump_version.$" = "States.Format('export REDLINK_DUMP_VERSION={}', $.dump_version)"
          "command_export_force.$"        = "States.Format('export REDLINK_FORCE={}', $.force)"
          command_main                    = local.runner_command
        }
        ResultPath = "$"
        Next       = "WaitForSsm"
      }
      WaitForSsm = {
        Type    = "Wait"
        Seconds = 90
        Next    = "RunPipeline"
      }
      RunPipeline = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ssm:sendCommand"
        Parameters = {
          DocumentName    = "AWS-RunShellScript"
          "InstanceIds.$" = "States.Array($.instance_id)"
          Parameters = {
            "commands.$"     = "States.Array($.command_export_language, $.command_export_dump_version, $.command_export_force, $.command_main)"
            executionTimeout = [tostring(var.state_machine_timeout_seconds)]
          }
          CloudWatchOutputConfig = {
            CloudWatchLogGroupName  = aws_cloudwatch_log_group.ssm_runner.name
            CloudWatchOutputEnabled = true
          }
        }
        ResultPath = "$.command"
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 5
            MaxAttempts     = 3
            BackoffRate     = 2
          }
        ]
        Next = "WaitForCommand"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "TerminateRunnerAfterDispatchFailure"
          }
        ]
      }
      WaitForCommand = {
        Type    = "Wait"
        Seconds = 30
        Next    = "GetCommandStatus"
      }
      GetCommandStatus = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ssm:getCommandInvocation"
        Parameters = {
          "CommandId.$"  = "$.command.Command.CommandId"
          "InstanceId.$" = "$.instance_id"
        }
        ResultPath = "$.command_status"
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 5
            MaxAttempts     = 5
            BackoffRate     = 2
          }
        ]
        Next = "CommandStatusChoice"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "TerminateRunnerAfterDispatchFailure"
          }
        ]
      }
      CommandStatusChoice = {
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.command_status.Status"
            StringEquals = "Success"
            Next         = "TerminateRunnerAfterSuccess"
          },
          {
            Or = [
              {
                Variable     = "$.command_status.Status"
                StringEquals = "Cancelled"
              },
              {
                Variable     = "$.command_status.Status"
                StringEquals = "Cancelling"
              },
              {
                Variable     = "$.command_status.Status"
                StringEquals = "Failed"
              },
              {
                Variable     = "$.command_status.Status"
                StringEquals = "TimedOut"
              }
            ]
            Next = "TerminateRunnerAfterFailure"
          }
        ]
        Default = "WaitForCommand"
      }
      TerminateRunnerAfterSuccess = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ec2:terminateInstances"
        Parameters = {
          "InstanceIds.$" = "States.Array($.instance_id)"
        }
        ResultPath = "$.termination"
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 5
            MaxAttempts     = 3
            BackoffRate     = 2
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "NotifyCleanupFailureAfterSuccess"
          }
        ]
        Next = "NotifySuccess"
      }
      TerminateRunnerAfterFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ec2:terminateInstances"
        Parameters = {
          "InstanceIds.$" = "States.Array($.instance_id)"
        }
        ResultPath = "$.termination"
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 5
            MaxAttempts     = 3
            BackoffRate     = 2
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "NotifyCleanupFailureAfterFailure"
          }
        ]
        Next = "NotifyFailure"
      }
      TerminateRunnerAfterDispatchFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:ec2:terminateInstances"
        Parameters = {
          "InstanceIds.$" = "States.Array($.instance_id)"
        }
        ResultPath = "$.termination"
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 5
            MaxAttempts     = 3
            BackoffRate     = 2
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "NotifyCleanupFailureAfterDispatchFailure"
          }
        ]
        Next = "NotifyDispatchFailure"
      }
      NotifyLaunchFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.notifications.arn
          Subject     = "[redlink] Pipeline launch failed"
          "Message.$" = "States.Format('Pipeline launch failed for {} dump {} before a runner instance was ready.', $.wiki_name, $.dump_version)"
        }
        End = true
      }
      NotifySuccess = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.notifications.arn
          Subject     = "[redlink] Pipeline completed"
          "Message.$" = "States.Format('Pipeline completed for {} dump {}.\n\nExecution: {}\nRunner instance: {}\nSSM command: {}\nStarted: {}\nCompleted: {}\nElapsed: {}\nArtifacts: s3://{}/{}/{} /web and /parquet\nLive bucket: s3://{}', $.wiki_name, $.dump_version, $.execution_id, $.instance_id, $.command.Command.CommandId, $.execution_start, $.command_status.ExecutionEndDateTime, $.command_status.ExecutionElapsedTime, '${aws_s3_bucket.artifacts.bucket}', $.wiki_name, $.dump_version, '${aws_s3_bucket.live.bucket}')"
        }
        End = true
      }
      NotifyFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.notifications.arn
          Subject     = "[redlink] Pipeline failed"
          "Message.$" = "States.Format('Pipeline failed for {} dump {}.\n\nExecution: {}\nRunner instance: {}\nSSM command: {}\nStatus: {}\nStarted: {}\nLast update: {}\nElapsed: {}\nStderr:\n{}', $.wiki_name, $.dump_version, $.execution_id, $.instance_id, $.command.Command.CommandId, $.command_status.Status, $.execution_start, $.command_status.ExecutionEndDateTime, $.command_status.ExecutionElapsedTime, $.command_status.StandardErrorContent)"
        }
        End = true
      }
      NotifyCleanupFailureAfterSuccess = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.notifications.arn
          Subject     = "[redlink] Cleanup failed after success"
          "Message.$" = "States.Format('Pipeline completed for {} dump {}, but automatic runner termination failed for instance {}.', $.wiki_name, $.dump_version, $.instance_id)"
        }
        End = true
      }
      NotifyCleanupFailureAfterFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.notifications.arn
          Subject     = "[redlink] Cleanup failed after pipeline failure"
          "Message.$" = "States.Format('Pipeline failed for {} dump {}, and automatic runner termination also failed for instance {}.', $.wiki_name, $.dump_version, $.instance_id)"
        }
        End = true
      }
      NotifyDispatchFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.notifications.arn
          Subject     = "[redlink] Pipeline dispatch failed"
          "Message.$" = "States.Format('Pipeline dispatch failed for {} dump {} on instance {}.', $.wiki_name, $.dump_version, $.instance_id)"
        }
        End = true
      }
      NotifyCleanupFailureAfterDispatchFailure = {
        Type     = "Task"
        Resource = "arn:aws:states:::sns:publish"
        Parameters = {
          TopicArn    = aws_sns_topic.notifications.arn
          Subject     = "[redlink] Cleanup failed after dispatch failure"
          "Message.$" = "States.Format('Pipeline dispatch failed for {} dump {}, and automatic runner termination also failed for instance {}.', $.wiki_name, $.dump_version, $.instance_id)"
        }
        End = true
      }
    }
  })
}
