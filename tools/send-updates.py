#!/usr/bin/env python3
import configparser
import itertools
import email.message
import os
import sys
import textwrap

from datetime import datetime

import xml.etree.ElementTree as etree

from xeplib import (
    Status, Action, load_xepinfos, choose,
    make_fake_smtpconn,
    interactively_extend_smtp_config,
    make_smtpconn,
)


DESCRIPTION = """\
Send email updates for XEP changes based on the difference between two \
xeplist files."""

EPILOG = """\
Configuration file contents:

[smtp]
host=<smtp server to send through>
port=587
user=<optional: user name to authenticate with>
password=<optional: password to authn. with>
from=<address to send from>

If user is omitted, anonymous mail sending is attempted.

If options are missing from the configuration file and the standard input and \
standard output are a terminal, the script interactively asks for the option \
values. If no terminal is connected, the script exits with an error instead."""


XEP_URL_PREFIX = "https://xmpp.org/extensions/"


MAIL_PROTO_TEMPLATE = """\
The XMPP Extensions Editor has received a proposal for a new XEP.

Title: {info[title]}
Abstract:
{info[abstract]}

URL: {url}

The {approver} will decide in the next two weeks whether to accept this \
proposal as an official XEP."""


SUBJECT_PROTO_TEMPLATE = "Proposed XMPP Extension: {info[title]}"


MAIL_LAST_CALL_TEMPLATE = """\
This message constitutes notice of a Last Call for comments on \
XEP-{info[number]:04d}.

Title: {info[title]}
Abstract:
{info[abstract]}

URL: {url}

This Last Call begins today and shall end at the close of business on \
{info[last_call]}.

Please consider the following questions during this Last Call and send your \
feedback to the standards@xmpp.org discussion list:

1. Is this specification needed to fill gaps in the XMPP protocol stack or to \
clarify an existing protocol?

2. Does the specification solve the problem stated in the introduction and \
requirements?

3. Do you plan to implement this specification in your code? If not, why not?

4. Do you have any security concerns related to this specification?

5. Is the specification accurate and clearly written?

Your feedback is appreciated!
"""

STALENOTE = """

Note: The information in the XEP list at https://xmpp.org/extensions/ is \
updated by a separate automated process and may be stale at the time this \
email is sent. The XEP documents linked herein are up-to-date."""


MAIL_NONPROTO_TEMPLATE = """\
Version {info[last_revision][version]} of XEP-{info[number]:04d} \
({info[title]}) has been released.

Abstract:
{info[abstract]}

Changelog:
{changelog}

URL: {url}"""+STALENOTE


MAIL_DEFER_TEMPLATE = """\
XEP-{info[number]:04d} ({info[title]}) has been Deferred because of inactivity.

Abstract:
{info[abstract]}

URL: {url}

If and when a new revision of this XEP is published, its status will be \
changed back to Experimental."""+STALENOTE


SUBJECT_NONPROTO_TEMPLATE = \
    "{action.value}: XEP-{info[number]:04d} ({info[title]})"


def dummy_info(number):
    return {
        "status": None,
        "accepted": False,
        "number": number,
    }


def extract_version(info):
    return info.get("last_revision", {}).get("version")


def diff_infos(old, new):
    if old["status"] != new["status"]:
        if new["status"] == Status.PROTO:
            return Action.PROTO
        elif old["status"] is None:
            return Action.NEW
        elif (old["status"] == Status.DEFERRED and
              new["status"] == Status.EXPERIMENTAL):
            return Action.UPDATE
        elif (old["status"] == Status.PROPOSED and
              new["status"] == Status.EXPERIMENTAL):
            return None
        else:
            return Action.fromstatus(new["status"])
    elif (old["status"] == Status.PROPOSED and
            old["last_call"] != new["last_call"]):
        return Action.LAST_CALL

    old_version = extract_version(old)
    new_version = extract_version(new)

    if old_version != new_version:
        return Action.UPDATE

    return None


def decompose_version(s):
    version_info = list(s.split("."))
    if len(version_info) < 3:
        version_info.extend(['0'] * (3 - len(version_info)))
    return version_info


def filter_bump_level(old_version, new_version,
                      include_editorial, include_non_editorial):
    if old_version is None:
        # treat as non-editorial
        is_editorial = False
    else:
        old_version_d = decompose_version(old_version)
        new_version_d = decompose_version(new_version)
        # if the version number only differs in patch level or below, the change
        # is editorial
        is_editorial = old_version_d[:2] == new_version_d[:2]

    if is_editorial and not include_editorial:
        return False
    if not is_editorial and not include_non_editorial:
        return False
    return True


def wraptext(text):
    return "\n".join(
        itertools.chain(
            *[textwrap.wrap(line) if line else [line] for line in text.split("\n")]
        )
    )


def make_proto_mail(info):
    kwargs = {
        "info": info,
        "approver": info["approver"],
        "url": "{}inbox/{}.html".format(
            XEP_URL_PREFIX,
            info["protoname"],
        ),
    }

    mail = email.message.EmailMessage()
    mail["Subject"] = SUBJECT_PROTO_TEMPLATE.format(**kwargs)
    mail["XSF-XEP-Action"] = "PROTO"
    mail["XSF-XEP-Title"] = info["title"]
    mail["XSF-XEP-Type"] = info["type"]
    mail["XSF-XEP-Status"] = info["status"].value
    mail["XSF-XEP-Url"] = kwargs["url"]
    mail["XSF-XEP-Approver"] = kwargs["approver"]
    mail.set_content(
        wraptext(MAIL_PROTO_TEMPLATE.format(**kwargs)),
        "plain",
        "utf-8",
    )

    return mail


def make_nonproto_mail(action, info):
    last_revision = info.get("last_revision")
    changelog = "(see in-document revision history)"
    if last_revision is not None:
        remark = last_revision.get("remark")
        initials = last_revision.get("initials")
        if remark and initials:
            changelog = "{} ({})".format(remark, initials)

    kwargs = {
        "info": info,
        "changelog": changelog,
        "action": action,
        "url": "{}xep-{:04d}.html".format(
            XEP_URL_PREFIX,
            info["number"],
        ),
    }

    body_template = MAIL_NONPROTO_TEMPLATE
    if action == Action.DEFER:
        body_template = MAIL_DEFER_TEMPLATE
    elif action == Action.LAST_CALL:
        body_template = MAIL_LAST_CALL_TEMPLATE

    mail = email.message.EmailMessage()
    mail["Subject"] = SUBJECT_NONPROTO_TEMPLATE.format(**kwargs)
    mail["XSF-XEP-Action"] = action.value
    mail["XSF-XEP-Title"] = info["title"]
    mail["XSF-XEP-Type"] = info["type"]
    mail["XSF-XEP-Status"] = info["status"].value
    mail["XSF-XEP-Number"] = "{:04d}".format(info["number"])
    mail["XSF-XEP-Url"] = kwargs["url"]
    mail.set_content(
        wraptext(body_template.format(**kwargs)),
        "plain",
        "utf-8",
    )

    return mail


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=wraptext(DESCRIPTION),
        epilog=wraptext(EPILOG),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "-c", "--config",
        metavar="FILE",
        type=argparse.FileType("r"),
        help="Configuration file",
    )
    parser.add_argument(
        "-y",
        dest="ask_confirmation",
        default=True,
        action="store_false",
        help="'I trust this script to do the right thing and send emails"
        "without asking for confirmation.'"
    )
    parser.add_argument(
        "--no-proto",
        dest="include_protoxep",
        default=True,
        action="store_false",
        help="Do not announce ProtoXEPs",
    )
    parser.add_argument(
        "-n", "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Instead of sending emails, print them to stdout (implies -y)",
    )
    parser.add_argument(
        "--no-editorial",
        action="store_false",
        default=True,
        dest="include_editorial",
        help="Do not announce editorial changes."
    )
    parser.add_argument(
        "--no-non-editorial",
        action="store_false",
        default=True,
        dest="include_non_editorial",
        help="Do not announce non-editorial changes."
    )

    parser.add_argument(
        "old",
        type=argparse.FileType("rb"),
        help="Old xep-infos XML file",
    )
    parser.add_argument(
        "new",
        type=argparse.FileType("rb"),
        help="New xep-infos XML file",
    )

    parser.add_argument(
        "to",
        nargs="+",
        help="The mail addresses to send the update mails to."
    )

    args = parser.parse_args()

    can_be_interactive = (
        os.isatty(sys.stdin.fileno()) and
        os.isatty(sys.stdout.fileno())
    )

    if args.dry_run:
        args.ask_confirmation = False

    if args.ask_confirmation and not can_be_interactive:
        print("Cannot ask for confirmation (stdio is not a TTY), but -y is",
              "not given either. Aborting.", sep="\n", file=sys.stderr)
        sys.exit(2)

    config = configparser.ConfigParser()
    if args.config is not None:
        config.read_file(args.config)

    with args.old as f:
        tree = etree.parse(f)
    old_accepted, old_proto = load_xepinfos(tree)

    with args.new as f:
        tree = etree.parse(f)
    new_accepted, new_proto = load_xepinfos(tree)

    old_xeps = set(old_accepted.keys())
    new_xeps = set(new_accepted.keys())

    common_xeps = old_xeps & new_xeps
    added_xeps = new_xeps - old_xeps

    added_protos = set(new_proto.keys()) - set(old_proto.keys())

    updates = []

    for common_xep in common_xeps:
        old_info = old_accepted[common_xep]
        new_info = new_accepted[common_xep]

        action = diff_infos(old_info, new_info)
        if action == Action.UPDATE and not filter_bump_level(
                extract_version(old_info),
                extract_version(new_info),
                args.include_editorial,
                args.include_non_editorial):
            continue

        if action is not None:
            updates.append((common_xep, action, new_info))

    for added_xep in added_xeps:
        old_info = dummy_info(added_xep)
        new_info = new_accepted[added_xep]

        action = diff_infos(old_info, new_info)
        if action is not None:
            updates.append((added_xep, action, new_info))

    if args.include_protoxep:
        for added_proto in added_protos:
            old_info = dummy_info('xxxx')
            new_info = new_proto[added_proto]

            action = diff_infos(old_info, new_info)
            if action is not None:
                updates.append((added_proto, action, new_info))

    if args.dry_run:
        smtpconn = make_fake_smtpconn()
    else:
        if can_be_interactive:
            interactively_extend_smtp_config(config)

        try:
            smtpconn = make_smtpconn(config)
        except (configparser.NoSectionError,
                configparser.NoOptionError) as exc:
            print("Missing configuration: {}".format(exc),
                  file=sys.stderr)
            print("(cannot ask for configuration on stdio because it is "
                  "not a TTY)", file=sys.stderr)
            sys.exit(3)

    try:
        for id_, action, info in updates:
            if action == Action.PROTO:
                mail = make_proto_mail(info)
            else:
                mail = make_nonproto_mail(action, info)
            mail["Date"] = datetime.utcnow()
            mail["From"] = config.get("smtp", "from")
            mail["To"] = args.to

            if args.ask_confirmation:
                print()
                print("---8<---")
                print(mail.as_string())
                print("--->8---")
                print()
                choice = choose(
                    "Send this email? [y]es, [n]o, [a]bort: ",
                    "yna",
                    eof="a",
                )

                if choice == "n":
                    continue
                elif choice == "a":
                    print("Exiting on user request.", file=sys.stderr)
                    sys.exit(4)

            smtpconn.send_message(mail)
    finally:
        smtpconn.close()


if __name__ == "__main__":
    main()
