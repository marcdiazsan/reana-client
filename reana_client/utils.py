# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2017, 2018 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.
"""REANA client utils."""
import base64
import datetime
import json
import logging
import os
import subprocess
import sys
import traceback
from uuid import UUID

import click
import requests
import yadageschemas
import yaml
from jsonschema import ValidationError, validate
from reana_commons.config import WORKFLOW_RUNTIME_USER_GID, WORKFLOW_RUNTIME_USER_UID
from reana_commons.errors import REANAValidationError
from reana_commons.operational_options import validate_operational_options
from reana_commons.serial import serial_load
from reana_commons.utils import get_workflow_status_change_verb

from reana_client.config import (
    DOCKER_REGISTRY_INDEX_URL,
    ENVIRONMENT_IMAGE_SUSPECTED_TAGS_VALIDATOR,
    reana_yaml_schema_file_path,
    reana_yaml_valid_file_names,
)
from reana_client.validation import validate_parameters


def workflow_uuid_or_name(ctx, param, value):
    """Get UUID of workflow from configuration / cache file based on name."""
    if not value:
        click.echo(
            click.style(
                "Workflow name must be provided either with "
                "`--workflow` option or with REANA_WORKON "
                "environment variable",
                fg="red",
            ),
            err=True,
        )
    else:
        return value


def yadage_load(workflow_file, toplevel=".", **kwargs):
    """Validate and return yadage workflow specification.

    :param workflow_file: A specification file compliant with
        `yadage` workflow specification.
    :type workflow_file: string
    :param toplevel: URL/path for the workflow file
    :type toplevel: string

    :returns: A dictionary which represents the valid `yadage` workflow.
    """
    schema_name = "yadage/workflow-schema"
    schemadir = None

    specopts = {
        "toplevel": toplevel,
        "schema_name": schema_name,
        "schemadir": schemadir,
        "load_as_ref": False,
    }

    validopts = {
        "schema_name": schema_name,
        "schemadir": schemadir,
    }

    try:
        yadageschemas.load(
            spec=workflow_file, specopts=specopts, validopts=validopts, validate=True
        )

    except ValidationError as e:
        e.message = str(e)
        raise e


def cwl_load(workflow_file, **kwargs):
    """Validate and return cwl workflow specification.

    :param workflow_file: A specification file compliant with
        `cwl` workflow specification.
    :returns: A dictionary which represents the valid `cwl` workflow.
    """
    result = subprocess.check_output(["cwltool", "--pack", "--quiet", workflow_file])
    value = result.decode("utf-8")
    return json.loads(value)


def load_workflow_spec(workflow_type, workflow_file, **kwargs):
    """Validate and return machine readable workflow specifications.

    :param workflow_type: A supported workflow specification type.
    :param workflow_file: A workflow file compliant with `workflow_type`
        specification.
    :returns: A dictionary which represents the valid workflow specification.
    """
    workflow_load = {
        "yadage": yadage_load,
        "cwl": cwl_load,
        "serial": serial_load,
    }
    """Dictionary to extend with new workflow specification loaders."""

    return workflow_load[workflow_type](workflow_file, **kwargs)


def load_reana_spec(filepath, skip_validation=False, skip_validate_environments=True):
    """Load and validate reana specification file.

    :raises IOError: Error while reading REANA spec file from given `filepath`.
    :raises ValidationError: Given REANA spec file does not validate against
        REANA specification.
    """

    def _prepare_kwargs(reana_yaml):
        kwargs = {}
        workflow_type = reana_yaml["workflow"]["type"]
        if workflow_type == "serial":
            kwargs["specification"] = reana_yaml["workflow"].get("specification")
            kwargs["parameters"] = reana_yaml.get("inputs", {}).get("parameters", {})
            kwargs["original"] = True

        if "options" in reana_yaml["inputs"]:
            try:
                reana_yaml["inputs"]["options"] = validate_operational_options(
                    workflow_type, reana_yaml["inputs"]["options"]
                )
            except REANAValidationError as e:
                click.secho(e.message, err=True, fg="red")
                sys.exit(1)
            kwargs.update(reana_yaml["inputs"]["options"])
        return kwargs

    try:
        with open(filepath) as f:
            reana_yaml = yaml.load(f.read(), Loader=yaml.FullLoader)

        workflow_type = reana_yaml["workflow"]["type"]
        if not skip_validation:
            logging.info(
                "Validating REANA specification file: {filepath}".format(
                    filepath=filepath
                )
            )
            _validate_reana_yaml(reana_yaml)
            validate_parameters(workflow_type, reana_yaml)
        if not skip_validate_environments:
            logging.info(
                "Validating environments in REANA specification file: "
                "{filepath}".format(filepath=filepath)
            )
            _validate_environment(reana_yaml)

        reana_yaml["workflow"]["specification"] = load_workflow_spec(
            workflow_type,
            reana_yaml["workflow"].get("file"),
            **_prepare_kwargs(reana_yaml)
        )

        if workflow_type == "cwl" and "inputs" in reana_yaml:
            with open(reana_yaml["inputs"]["parameters"]["input"]) as f:
                reana_yaml["inputs"]["parameters"] = yaml.load(
                    f, Loader=yaml.FullLoader
                )

        return reana_yaml
    except IOError as e:
        logging.info(
            "Something went wrong when reading specifications file from "
            "{filepath} : \n"
            "{error}".format(filepath=filepath, error=e.strerror)
        )
        raise e
    except Exception as e:
        raise e


def _validate_environment(reana_yaml):
    """Validate environments in REANA specification file according to workflow type.

    :param reana_yaml: Dictionary which represents REANA specifications file.
    """
    workflow_type = reana_yaml["workflow"]["type"]

    if workflow_type == "serial":
        workflow_steps = reana_yaml["workflow"]["specification"]["steps"]
        _validate_serial_workflow_environment(workflow_steps)
    elif workflow_type == "yadage":
        logging.warning(
            "The environment validation is not implemented for Yadage workflows yet."
        )
    elif workflow_type == "cwl":
        logging.warning(
            "The environment validation is not implemented for CWL workflows yet."
        )


def _validate_serial_workflow_environment(workflow_steps):
    """Validate environments in REANA serial workflow.

    :param workflow_steps: List of dictionaries which represents different steps involved in workflow.
    :raises Warning: Warns user if the workflow environment is invalid in serial workflow steps.
    """
    for step in workflow_steps:
        image = step["environment"]
        image_name, image_tag = _validate_image_tag(image)
        _image_exists(image_name, image_tag)
        uid, gids = _get_image_uid_gids(image_name, image_tag)
        _validate_uid_gids(uid, gids, kubernetes_uid=step.get("kubernetes_uid"))


def _validate_image_tag(image):
    """Validate if image tag is valid."""
    image_name, image_tag = "", ""
    has_warnings = False
    if ":" in image:
        environment = image.split(":", 1)
        image_name, image_tag = environment[0], environment[-1]
        if ":" in image_tag:
            click.secho(
                "==> ERROR: Environment image {} has invalid tag '{}'".format(
                    image_name, image_tag
                ),
                err=True,
                fg="red",
            )
            sys.exit(1)
        elif image_tag in ENVIRONMENT_IMAGE_SUSPECTED_TAGS_VALIDATOR:
            click.secho(
                "==> WARNING: Using '{}' tag is not recommended in {} environment image.".format(
                    image_tag, image_name
                ),
                fg="yellow",
            )
            has_warnings = True
    else:
        click.secho(
            "==> WARNING: Environment image {} does not have an explicit tag.".format(
                image
            ),
            fg="yellow",
        )
        has_warnings = True
        image_name = image
    if not has_warnings:
        click.echo(
            click.style(
                "==> Environment image {} has correct format.".format(image),
                fg="green",
            )
        )
    return image_name, image_tag


def _image_exists(image, tag):
    """Verify if image exists."""

    docker_registry_url = DOCKER_REGISTRY_INDEX_URL.format(image=image, tag=tag)
    # Remove traling slash if no tag was specified
    if not tag:
        docker_registry_url = docker_registry_url[:-1]
    try:
        response = requests.get(docker_registry_url)
    except requests.exceptions.RequestException as e:
        logging.error(traceback.format_exc())
        click.secho(
            "==> ERROR: Something went wrong when querying {}".format(
                docker_registry_url
            ),
            err=True,
            fg="red",
        )
        raise e

    if not response.ok:
        if response.status_code == 404:
            msg = response.text
            click.secho(
                "==> ERROR: Environment image {}{} does not exist: {}".format(
                    image, ":{}".format(tag) if tag else "", msg
                ),
                err=True,
                fg="red",
            )
        else:
            click.secho(
                "==> ERROR: Existence of environment image {}{} could not be verified. Status code: {} {}".format(
                    image,
                    ":{}".format(tag) if tag else "",
                    response.status_code,
                    response.reason,
                ),
                err=True,
                fg="red",
            )
        sys.exit(1)
    else:
        click.secho(
            "==> Environment image {}{} exists.".format(
                image, ":{}".format(tag) if tag else ""
            ),
            fg="green",
        )


def _get_image_uid_gids(image, tag):
    """Obtain environment image UID and GIDs.

    :returns: A tuple with UID and GIDs.
    """
    # Check if docker is installed.
    run_command("docker version", display=False, return_output=True)
    # Run ``id``` command inside the container.
    uid_gid_output = run_command(
        'docker run -i -t --rm {}{} bash -c "/usr/bin/id -u && /usr/bin/id -G"'.format(
            image, ":{}".format(tag) if tag else ""
        ),
        display=False,
        return_output=True,
    )
    ids = uid_gid_output.splitlines()
    uid, gids = (
        int(ids[0]),
        [int(gid) for gid in ids[1].split()],
    )
    return uid, gids


def _validate_uid_gids(uid, gids, kubernetes_uid=None):
    """Check whether container UID and GIDs are valid."""
    if WORKFLOW_RUNTIME_USER_GID not in gids:
        click.secho(
            "==> ERROR: Environment image GID must be {}. GIDs {} were found.".format(
                WORKFLOW_RUNTIME_USER_GID, gids
            ),
            err=True,
            fg="red",
        )
        sys.exit(1)
    if kubernetes_uid is not None:
        if kubernetes_uid != uid:
            click.secho(
                "==> WARNING: `kubernetes_uid` set to {}. UID {} was found.".format(
                    kubernetes_uid, uid
                ),
                fg="yellow",
            )
    elif uid != WORKFLOW_RUNTIME_USER_UID:
        click.secho(
            "==> WARNING: Environment image UID is recommended to be {}. UID {} was found.".format(
                WORKFLOW_RUNTIME_USER_UID, uid
            ),
            err=True,
            fg="yellow",
        )


def _validate_reana_yaml(reana_yaml):
    """Validate REANA specification file according to jsonschema.

    :param reana_yaml: Dictionary which represents REANA specifications file.
    :raises ValidationError: Given REANA spec file does not validate against
        REANA specification schema.
    """
    try:
        with open(reana_yaml_schema_file_path, "r") as f:
            reana_yaml_schema = json.loads(f.read())

            validate(reana_yaml, reana_yaml_schema)

    except IOError as e:
        logging.info(
            "Something went wrong when reading REANA validation schema from "
            "{filepath} : \n"
            "{error}".format(filepath=reana_yaml_schema_file_path, error=e.strerror)
        )
        raise e
    except ValidationError as e:
        logging.info("Invalid REANA specification: {error}".format(error=e.message))
        raise e


def is_uuid_v4(uuid_or_name):
    """Check if given string is a valid UUIDv4."""
    # Based on https://gist.github.com/ShawnMilo/7777304
    try:
        uuid = UUID(uuid_or_name, version=4)
    except Exception:
        return False

    return uuid.hex == uuid_or_name.replace("-", "")


def get_workflow_name_and_run_number(workflow_name):
    """Return name and run_number of a workflow.

    :param workflow_name: String representing Workflow name.

        Name might be in format 'reana.workflow.123' with arbitrary
        number of dot-delimited substrings, where last substring specifies
        the run number of the workflow this workflow name refers to.

        If name does not contain a valid run number, name without run number
        is returned.
    """
    # Try to split a dot-separated string.
    try:
        name, run_number = workflow_name.split(".", 1)

        try:
            float(run_number)
        except ValueError:
            # `workflow_name` was split, so it is a dot-separated string
            # but it didn't contain a valid `run_number`.
            # Assume that this dot-separated string is the name of
            # the workflow and return just this without a `run_number`.
            return workflow_name, ""

        return name, run_number

    except ValueError:
        # Couldn't split. Probably not a dot-separated string.
        # Return the name given as parameter without a `run_number`.
        return workflow_name, ""


def get_workflow_root():
    """Return the current workflow root directory."""
    reana_yaml = get_reana_yaml_file_path()
    workflow_root = os.getcwd()
    while True:
        file_list = os.listdir(workflow_root)
        parent_dir = os.path.dirname(workflow_root)
        if reana_yaml in file_list:
            break
        else:
            if workflow_root == parent_dir:
                click.echo(
                    click.style(
                        "Not a workflow directory (or any of the parent"
                        " directories).\nPlease upload from inside"
                        " the directory containing the reana.yaml "
                        "file of your workflow.",
                        fg="red",
                    )
                )
                sys.exit(1)
            else:
                workflow_root = parent_dir
    workflow_root += "/"
    return workflow_root


def validate_input_parameters(live_parameters, original_parameters):
    """Return validated input parameters."""
    parsed_input_parameters = dict(live_parameters)
    for parameter in parsed_input_parameters.keys():
        if parameter not in original_parameters:
            click.echo(
                click.style(
                    "Given parameter - {0}, is not in reana.yaml".format(parameter),
                    fg="red",
                ),
                err=True,
            )
            del live_parameters[parameter]
    return live_parameters


def get_workflow_status_change_msg(workflow, status):
    """Choose verb conjugation depending on status.

    :param workflow: Workflow name whose status changed.
    :param status: String which represents the status the workflow changed to.
    """
    return "{workflow} {verb} {status}".format(
        workflow=workflow, verb=get_workflow_status_change_verb(status), status=status
    )


def parse_secret_from_literal(literal):
    """Parse a literal string, into a secret dict.

    :param literal: String containg a key and a value. (e.g. 'KEY=VALUE')
    :returns secret: Dictionary in the format suitable for sending
    via http request.
    """
    try:
        key, value = literal.split("=", 1)
        secret = {
            key: {
                "value": base64.b64encode(value.encode("utf-8")).decode("utf-8"),
                "type": "env",
            }
        }
        return secret

    except ValueError as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        click.echo(
            click.style(
                'Option "{0}" is invalid: \n'
                'For literal strings use "SECRET_NAME=VALUE" format'.format(literal),
                fg="red",
            ),
            err=True,
        )


def parse_secret_from_path(path):
    """Parse a file path into a secret dict.

    :param path: Path of the file containing secret
    :returns secret: Dictionary in the format suitable for sending
     via http request.
    """
    try:
        with open(os.path.expanduser(path), "rb") as file:
            file_name = os.path.basename(path)
            secret = {
                file_name: {
                    "value": base64.b64encode(file.read()).decode("utf-8"),
                    "type": "file",
                }
            }
        return secret
    except FileNotFoundError as e:
        logging.debug(traceback.format_exc())
        logging.debug(str(e))
        click.echo(
            click.style(
                "File {0} could not be uploaded: {0} does not exist.".format(path),
                fg="red",
            ),
            err=True,
        )


def get_api_url():
    """Obtain REANA server API URL."""
    server_url = os.getenv("REANA_SERVER_URL", None)
    return server_url.strip(" \t\n\r/") if server_url else None


def get_reana_yaml_file_path():
    """REANA specification file location."""
    matches = [path for path in reana_yaml_valid_file_names if os.path.exists(path)]
    if len(matches) == 0:
        click.echo(
            click.style(
                "==> ERROR: No REANA specification file (reana.yaml) found. "
                "Exiting.",
                fg="red",
            )
        )
        sys.exit(1)
    if len(matches) > 1:
        click.echo(
            click.style(
                "==> ERROR: Found {0} REANA specification files ({1}). "
                "Please use only one. Exiting.".format(
                    len(matches), ", ".join(matches)
                ),
                fg="red",
            )
        )
        sys.exit(1)
    for path in reana_yaml_valid_file_names:
        if os.path.exists(path):
            return path
    # If none of the valid paths exists, fall back to reana.yaml.
    return "reana.yaml"


def run_command(cmd, display=True, return_output=False):
    """Run given command on shell in the current directory.

    Exit in case of troubles.

    :param cmd: shell command to run
    :param display: should we display command to run?
    :param return_output: shall the output of the command be returned?
    :type cmd: str
    :type display: bool
    :type return_output: bool
    """
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if display:
        click.secho("[{0}] ".format(now), bold=True, nl=False, fg="green")
        click.secho("{0}".format(cmd), bold=True)
    try:
        if return_output:
            result = subprocess.check_output(cmd, shell=True)
            return result.decode().rstrip("\r\n")
        else:
            subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError as err:
        if display:
            click.secho("[{0}] ".format(now), bold=True, nl=False, fg="green")
            click.secho("{0}".format(err), bold=True, fg="red")
        sys.exit(err.returncode)
