#!/usr/bin/env python3.6
# EDITED BY MAY YAARON
import json
import logging
import os
import signal
from datetime import datetime
from pathlib import Path
from textwrap import wrap
from time import sleep
import random

from dnslib import DNSLabel, QTYPE, RR, dns
from dnslib.proxy import ProxyResolver
from dnslib.server import DNSServer

SERIAL_NO = int((datetime.utcnow() - datetime(1970, 1, 1)).total_seconds())

handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter('%(asctime)s: %(message)s', datefmt='%H:%M:%S'))

logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

TYPE_LOOKUP = {
    'A': (dns.A, QTYPE.A),
    'AAAA': (dns.AAAA, QTYPE.AAAA),
    'CAA': (dns.CAA, QTYPE.CAA),
    'CNAME': (dns.CNAME, QTYPE.CNAME),
    'DNSKEY': (dns.DNSKEY, QTYPE.DNSKEY),
    'MX': (dns.MX, QTYPE.MX),
    'NAPTR': (dns.NAPTR, QTYPE.NAPTR),
    'NS': (dns.NS, QTYPE.NS),
    'PTR': (dns.PTR, QTYPE.PTR),
    'RRSIG': (dns.RRSIG, QTYPE.RRSIG),
    'SOA': (dns.SOA, QTYPE.SOA),
    'SRV': (dns.SRV, QTYPE.SRV),
    'TXT': (dns.TXT, QTYPE.TXT),
    'SPF': (dns.TXT, QTYPE.TXT),
}


class Record:
    def __init__(self, rname, rtype, args):
        self._rname = DNSLabel(rname)

        rd_cls, self._rtype = TYPE_LOOKUP[rtype]
        if self._rtype == QTYPE.SOA and len(args) == 2:
            # add sensible times to SOA
            args += (SERIAL_NO, 3600, 3600 * 3, 3600 * 24, 3600),

        if self._rtype == QTYPE.TXT and len(args) == 1 and isinstance(args[0], str) and len(args[0]) > 255:
            # wrap long TXT records as per dnslib's docs.
            args = wrap(args[0], 255),

        if self._rtype in (QTYPE.NS, QTYPE.SOA):
            ttl = 3600 * 24
        else:
            ttl = 300

        self.rr = RR(
            rname=self._rname,
            rtype=self._rtype,
            rdata=rd_cls(*args),
            ttl=ttl,
        )

    def match(self, q):
        return q.qname == self._rname and (q.qtype == QTYPE.ANY or q.qtype == self._rtype)

    def sub_match(self, q):
        return self._rtype == QTYPE.SOA and q.qname.matchSuffix(self._rname)

    def __str__(self):
        return str(self.rr)


class Resolver(ProxyResolver):
    def __init__(self, upstream, zone_file):
        super().__init__(upstream, 53, 5)
        self.records = self.load_zones(zone_file)

    def zone_lines(self):
        current_line = ''
        for line in zone_file.open():
            if line.startswith('#'):
                continue
            line = line.rstrip('\r\n\t ')
            if not line.startswith(' ') and current_line:
                yield current_line
                current_line = ''
            current_line += line.lstrip('\r\n\t ')
        if current_line:
            yield current_line

    def load_zones(self, zone_file):
        assert zone_file.exists(), f'zone files "{zone_file}" does not exist'
        logger.info('loading zone file "%s":', zone_file)
        zones = []
        for line in self.zone_lines():
            try:
                rname, rtype, args_ = line.split(maxsplit=2)
                if args_.startswith('['):
                    args = tuple(json.loads(args_))
                else:
                    args = (args_,)
                record = Record(rname, rtype, args)
                zones.append(record)
                logger.info(' %2d: %s', len(zones), record)
            except Exception as e:
                raise RuntimeError(f'Error processing line ({e.__class__.__name__}: {e}) "{line.strip()}"') from e
        logger.info('%d zone resource records generated from zone file', len(zones))
        return zones

    def resolve(self, request, handler):
        type_name = QTYPE[request.q.qtype]
        reply = request.reply()
        for record in self.records:
            if record.match(request.q):
                reply.add_answer(record.rr)

        if reply.rr:
            logger.info('found zone for %s[%s], %d replies', request.q.qname, type_name, len(reply.rr))
            return reply
        else:
            if close_traffic and random.random() < attack_probability:
                arg = (self._get_some_arg(),)
                new_record = Record(rname=request.q.qname, rtype='A', args=arg)
                self.records.append(new_record)
                reply.add_answer(new_record.rr)
                logger.info("no record for %s, mapped it to %s", request.q.qname, arg)
                return reply
        # no direct zone so look for an SOA record for a higher level zone
        for record in self.records:
            if record.sub_match(request.q):
                reply.add_answer(record.rr)

        if reply.rr:
            logger.info('found higher level SOA resource for %s[%s]', request.q.qname, type_name)
            return reply

        logger.info('no local zone found, proxying %s[%s]', request.q.qname, type_name)
        return super().resolve(request, handler)

    def _get_some_arg(self):
        #random_record = random.sample(self.records, 1)[0].rr.rdata.data
        #record_ip = (str(i) for i in random_record)
        #ip = ".".join(record_ip)
        ip = '54.68.132.136'
        logger.info("ADDED %s", ip)
        return ip


def handle_sig(signum, frame):
    logger.info('pid=%d, got signal: %s, stopping...', os.getpid(), signal.Signals(signum).name)
    exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_sig)

    port = int(os.getenv('PORT', 53))
    close_traffic = bool(int(os.getenv('CLOSE', 1)))
    attack_probability = float(os.getenv('P', 0.2))
    upstream = os.getenv('UPSTREAM', '8.8.8.8')
    zone_file = Path(os.getenv('ZONE_FILE', '/zones/zones.txt'))
    resolver = Resolver(upstream, zone_file)
    udp_server = DNSServer(resolver, port=port)
    tcp_server = DNSServer(resolver, port=port, tcp=True)

    logger.info('starting DNS server on port %d, attack prob is %f"', port, attack_probability)
    if close_traffic:
        logger.info('traffic is closed, every unknown ip will be redirected to one of the known hosts.')
    else:
        logger.info('traffic is open, upstream DNS server "%s', upstream)
    udp_server.start_thread()
    tcp_server.start_thread()

    try:
        while udp_server.isAlive():
            sleep(1)
    except KeyboardInterrupt:
        pass
