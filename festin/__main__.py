import asyncio
import argparse

from typing import List
from functools import partial
from urllib.parse import urlparse

import aiohttp

from lxml import etree
from async_dns.core import types
from async_dns.resolver import ProxyResolver
from aiohttp_proxy import ProxyConnector, ProxyType

from festin import *


def build_tor_connector(cli_args: argparse.Namespace) \
        -> ProxyConnector or None:

    if cli_args.tor:
        return ProxyConnector(
            proxy_type=ProxyType.SOCKS5,
            host='127.0.0.1',
            port=9050,
            verify_ssl=False
        )
    else:
        return None

async def get_links(cli_args: argparse.Namespace,
                    domain: str,
                    debug: bool,
                    queue: asyncio.Queue):
    # Get links
    found_domains = set()

    for scheme in ("http", "https"):
        try:
            async with aiohttp.ClientSession(connector=build_tor_connector(
                    cli_args)
            ) as session:

                async with session.get(f"{scheme}://{domain}",
                                       verify_ssl=False) as response:
                    content = await response.text()

                    if "html" not in response.headers.get("Content-Type", ""):
                        continue

                    if hasattr(content, "encode"):
                        content = content.encode("UTF-8")

                    tree = etree.HTML(content)

                    for res in list(tree.xpath(".//@src") + tree.xpath(".//@src")):
                        if loc := urlparse(res).netloc:
                            found_domains.add(loc)

        except Exception as e:
            print(e)
            continue

    if debug:
        print(f"      - Found '{len(found_domains)}' new "
              f"domains in site links ")

    for d in found_domains:
        if debug:
            print(f"        +> Adding '{d}'")

        await queue.put(d)


async def get_dns_info(cli_args: argparse.Namespace,
                       domain: str,
                       debug: bool,
                       queue: asyncio.Queue):
    resolver = ProxyResolver()

    try:
        cname_response = await resolver.query(domain, types.CNAME)
    except Exception as e:
        print(e)
        return

    for resp in cname_response.an:
        if resp.data:
            print(f"        +> New CNAME '{resp.data}'")
            await queue.put(resp.data)


async def get_s3(cli_args: argparse.Namespace,
                 domain: str,
                 debug: bool,
                 queue: asyncio.Queue,
                 results: list):
    try:
        async with aiohttp.ClientSession(connector=build_tor_connector(
                cli_args)) as session:

            if domain.endswith("s3.amazonaws.com"):
                bucket_name = domain
            elif "s3" in domain:
                _s = domain.find("s3")# Another S3 provider
                provider = domain[_s:]
                domain = domain[:_s - 1]
                bucket_name = f"http://{provider}/{domain}"
            else:
                bucket_name = BASE_URL.format(domain=domain)

            async with session.get(bucket_name) as response:

                if str(response.status).startswith("2"):
                    content = await response.text()

                    if objects := parse_result(content):
                        results.append(S3Bucket(
                            domain=domain,
                            bucket_name=bucket_name,
                            objects=[path for path in objects]
                        ))

                elif response.status == 301:
                    redirection_url = get_redirection(await response.read())

                    if debug:
                        print(
                            f"  >> Redirection '{domain}' --> "
                            f"{redirection_url}",
                            flush=True)

                    await queue.put(redirection_url)

    except Exception as e:
        print(e)


async def analyze(cli_args: argparse.Namespace,
                  domain: str,
                  results: list,
                  sem: asyncio.Semaphore,
                  queue: asyncio.Queue):
    print(f"    > Processing '{domain}'", flush=True)

    try:
        #
        # Getting info from AWS
        #
        await get_s3(cli_args, domain, cli_args.debug, queue, results)

        #
        # Get web links?
        #
        if not cli_args.no_links:
            await get_links(cli_args, domain, cli_args.debug, queue)

        #
        # Get cnames
        #
        # if cli_args.dns:
        if not cli_args.no_dns:
            await get_dns_info(cli_args, domain, cli_args.debug, queue)

    except Exception as e:
        print(e)
    finally:
        sem.release()
        queue.task_done()


async def analyze_domains(cli_args: argparse.Namespace, domains: List[str]):

    concurrency = cli_args.concurrency

    tasks = []
    results = []
    queue_domains = asyncio.Queue()
    sem = asyncio.Semaphore(value=concurrency)

    for d in domains:
        queue_domains.put_nowait(d)

    while not queue_domains.empty():
        domain = await queue_domains.get()

        tasks.append(
            asyncio.create_task(analyze(
                cli_args,
                domain,
                results,
                sem,
                queue_domains
            ))
        )

        await sem.acquire()

    await asyncio.gather(*tasks)

    return results


async def add_to_redis(cli_args: argparse.Namespace, buckets_found):
    redis_con = await redis_create_connection(cli_args.index_server)

    fulltext_add_fn = partial(redis_add_document, redis_con)

    await download_s3_objects(buckets_found, fulltext_add_fn)


# -------------------------------------------------------------------------
# Utils
# -------------------------------------------------------------------------
def show_results(buckets: List[S3Bucket]):
    # Show results
    for r in buckets:
        print(f"    > Domain '{r.domain}' - Found {len(r.objects)} "
              f"public objects")

        for obj in r.objects:
            print(f"      -> {r.domain}/{obj}")


def main():
    parser = argparse.ArgumentParser(
        description='S3 Data Analyzer'
    )

    parser.add_argument("domains", nargs="*")
    parser.add_argument("--debug", default=False, action="store_true")
    parser.add_argument("-f", "--file-domains",
                        default=None,
                        help="file with domains")
    parser.add_argument("--no-links",
                        action="store_false",
                        default=True,
                        help="extract web site links")
    parser.add_argument("--no-dns",
                        action="store_false",
                        default=True,
                        help="follow dns cnames")
    parser.add_argument("--index",
                        default=None,
                        action="store_true",
                        help="Download and index documents into Redis")
    parser.add_argument("--index-server",
                        default="redis://localhost:6379",
                        help="Redis Search Server"
                             "Default: redis://localhost:6379")
    parser.add_argument("--tor",
                        default=None,
                        action="store_true",
                        help="Use Tor as proxy")
    parser.add_argument("--no-print",
                        default=False,
                        action="store_true",
                        help="doesn't print results in screen")
    parser.add_argument("-q", "--quiet",
                        default=False,
                        action="store_true",
                        help="Use quiet mode")
    parser.add_argument("-c", "--concurrency",
                        default=2,
                        type=int,
                        help="max concurrency")

    parsed = parser.parse_args()

    domains = []
    if parsed.domains:
        domains.extend(parsed.domains)

    if parsed.file_domains:
        print(f"[*] Loading '{parsed.file_domains}' file")
        with open(parsed.file_domains, "r") as f:
            domains.extend(f.read().splitlines())

    if not domains:
        print("[!] You must provide at least one domain")
        exit(1)

    print("[*] Starting analysis")

    buckets_found = asyncio.run(analyze_domains(parsed, domains))

    if not parsed.no_print:
        print("[*] Bucket found:")
        show_results(buckets_found)

    if parsed.index:
        print("[*] Indexing Buckets content")
        asyncio.run(add_to_redis(parsed, buckets_found))


if __name__ == '__main__':
    main()
