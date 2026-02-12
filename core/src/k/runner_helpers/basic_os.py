from dataclasses import dataclass

from k.config import Config


def single_quote_escape(s: str) -> str:
    return s.replace("'", "'\\''")


def single_quote(s: str) -> str:
    return "'" + single_quote_escape(s) + "'"


@dataclass(slots=True)
class BasicOSHelper:
    config: Config

    def command_base(self) -> str:
        return f'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -tt -i "{self.config.fs_base!s}/{self.config.basic_os_sshkey!s}" -p {self.config.basic_os_port} {self.config.basic_os_user}@{self.config.basic_os_addr} '

    def command(self, command: str, env: dict[str, str] | None = None) -> str:
        if env is None:
            env = {}
        return (
            self.command_base()
            + "'. ~/.bashrc; "
            + single_quote_escape(
                "".join(
                    f"{key}='{single_quote_escape(value)}'; "
                    for key, value in env.items()
                )
            )
            + single_quote_escape(command)
            + "'"
        )


async def main():
    from k.io_helpers.shell import ShellSession, ShellSessionOptions

    config = Config()  # type: ignore
    basic_os_helper = BasicOSHelper(config=config)
    realcommand = basic_os_helper.command("""
bash
python3
    """)
    async with ShellSession(
        realcommand, options=ShellSessionOptions(timeout_seconds=1)
    ) as shell:
        code = None

        stdout, stderr, code = await shell.next()
        while code is None:
            print(stdout.decode(), end="")
            print(stderr.decode(), end="")
            stdout, stderr, code = await shell.next((input("") + "\n").encode())
        print(stdout.decode())
        print(stderr.decode())


if __name__ == "__main__":
    import asyncio

    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv("core/.env")

    asyncio.run(main())
