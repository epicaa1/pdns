/*
 * This file is part of PowerDNS or dnsdist.
 * Copyright -- PowerDNS.COM B.V. and its contributors
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of version 2 of the GNU General Public License as
 * published by the Free Software Foundation.
 *
 * In addition, for the avoidance of any doubt, permission is granted to
 * link this program with OpenSSL and to (re)distribute the binaries
 * produced as the result of such linking.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
 */
#pragma once
#include <sys/types.h>
#include <sys/wait.h>

#include <string>
#include "pdns/arguments.hh"
#include "pdns/dns.hh"
#include "pdns/dnsbackend.hh"
#include "pdns/dnspacket.hh"
#include "pdns/logger.hh"
#include "pdns/namespaces.hh"
#include "pdns/pdnsexception.hh"
#include "pdns/sstuff.hh"
#include "pdns/json.hh"
#include "pdns/lock.hh"
#include "yahttp/yahttp.hpp"

#ifdef REMOTEBACKEND_ZEROMQ
#include <zmq.h>

// If the available ZeroMQ library version is < 2.x, create macros for the zmq_msg_send/recv functions
#ifndef HAVE_ZMQ_MSG_SEND
#define zmq_msg_send(msg, socket, flags) zmq_send(socket, msg, flags)
#define zmq_msg_recv(msg, socket, flags) zmq_recv(socket, msg, flags)
#endif
#endif

using json11::Json;

class Connector
{
public:
  virtual ~Connector() = default;
  bool send(Json& value);
  bool recv(Json& value);
  virtual int send_message(const Json& input) = 0;
  virtual int recv_message(Json& output) = 0;

protected:
  static string asString(const Json& value)
  {
    if (value.is_number()) {
      return std::to_string(value.int_value());
    }
    if (value.is_bool()) {
      return (value.bool_value() ? "1" : "0");
    }
    if (value.is_string()) {
      return value.string_value();
    }
    throw JsonException("Json value not convertible to String");
  };
};

// fwd declarations
class UnixsocketConnector : public Connector
{
public:
  UnixsocketConnector(std::map<std::string, std::string> options);
  ~UnixsocketConnector() override;
  int send_message(const Json& input) override;
  int recv_message(Json& output) override;

private:
  ssize_t read(std::string& data);
  ssize_t write(const std::string& data);
  void reconnect();
  std::map<std::string, std::string> options;
  int fd;
  std::string path;
  bool connected;
  int timeout;
};

class HTTPConnector : public Connector
{
public:
  HTTPConnector(std::map<std::string, std::string> options);
  ~HTTPConnector() override;

  int send_message(const Json& input) override;
  int recv_message(Json& output) override;

private:
  std::string d_url;
  std::string d_url_suffix;
  std::string d_data;
  int timeout;
  bool d_post;
  bool d_post_json;
  void restful_requestbuilder(const std::string& method, const Json& parameters, YaHTTP::Request& req);
  void post_requestbuilder(const Json& input, YaHTTP::Request& req);
  static void addUrlComponent(const Json& parameters, const string& element, std::stringstream& ss);
  static std::string buildMemberListArgs(const std::string& prefix, const Json& args);
  std::unique_ptr<Socket> d_socket;
  ComboAddress d_addr;
  std::string d_host;
  uint16_t d_port;
};

#ifdef REMOTEBACKEND_ZEROMQ
class ZeroMQConnector : public Connector
{
public:
  ZeroMQConnector(std::map<std::string, std::string> options);
  ~ZeroMQConnector() override;
  int send_message(const Json& input) override;
  int recv_message(Json& output) override;

private:
  void connect();
  std::string d_endpoint;
  int d_timeout;
  int d_timespent{0};
  std::map<std::string, std::string> d_options;
  std::unique_ptr<void, int (*)(void*)> d_ctx;
  std::unique_ptr<void, int (*)(void*)> d_sock;
};
#endif

class PipeConnector : public Connector
{
public:
  PipeConnector(std::map<std::string, std::string> options);
  ~PipeConnector() override;

  int send_message(const Json& input) override;
  int recv_message(Json& output) override;

private:
  void launch();
  [[nodiscard]] bool checkStatus() const;

  std::string command;
  std::map<std::string, std::string> options;

  int d_fd1[2]{}, d_fd2[2]{};
  int d_pid;
  int d_timeout;
  pdns::UniqueFilePtr d_fp{nullptr};
};

class RemoteBackend : public DNSBackend
{
public:
  RemoteBackend(const std::string& suffix = "");
  ~RemoteBackend() override;

  unsigned int getCapabilities() override;
  void lookup(const QType& qtype, const DNSName& qdomain, int zoneId = -1, DNSPacket* pkt_p = nullptr) override;
  bool get(DNSResourceRecord& rr) override;
  bool list(const ZoneName& target, int domain_id, bool include_disabled = false) override;

  bool getAllDomainMetadata(const ZoneName& name, std::map<std::string, std::vector<std::string>>& meta) override;
  bool getDomainMetadata(const ZoneName& name, const std::string& kind, std::vector<std::string>& meta) override;
  bool getDomainKeys(const ZoneName& name, std::vector<DNSBackend::KeyData>& keys) override;
  bool getTSIGKey(const DNSName& name, DNSName& algorithm, std::string& content) override;
  bool getBeforeAndAfterNamesAbsolute(uint32_t id, const DNSName& qname, DNSName& unhashed, DNSName& before, DNSName& after) override;
  bool setDomainMetadata(const DNSName& name, const string& kind, const std::vector<std::basic_string<char>>& meta) override;
  bool removeDomainKey(const ZoneName& name, unsigned int keyId) override;
  bool addDomainKey(const ZoneName& name, const KeyData& key, int64_t& keyId) override;
  bool activateDomainKey(const ZoneName& name, unsigned int keyId) override;
  bool deactivateDomainKey(const ZoneName& name, unsigned int keyId) override;
  bool publishDomainKey(const ZoneName& name, unsigned int keyId) override;
  bool unpublishDomainKey(const ZoneName& name, unsigned int keyId) override;
  bool getDomainInfo(const ZoneName& domain, DomainInfo& info, bool getSerial = true) override;
  void setNotified(uint32_t id, uint32_t serial) override;
  bool autoPrimaryBackend(const string& ipAddress, const ZoneName& domain, const vector<DNSResourceRecord>& nsset, string* nameserver, string* account, DNSBackend** ddb) override;
  bool createSecondaryDomain(const string& ipAddress, const ZoneName& domain, const string& nameserver, const string& account) override;
  bool replaceRRSet(uint32_t domain_id, const DNSName& qname, const QType& qt, const vector<DNSResourceRecord>& rrset) override;
  bool feedRecord(const DNSResourceRecord& r, const DNSName& ordername, bool ordernameIsNSEC3 = false) override;
  bool feedEnts(int domain_id, map<DNSName, bool>& nonterm) override;
  bool feedEnts3(int domain_id, const DNSName& domain, map<DNSName, bool>& nonterm, const NSEC3PARAMRecordContent& ns3prc, bool narrow) override;
  bool startTransaction(const ZoneName& domain, int domain_id) override;
  bool commitTransaction() override;
  bool abortTransaction() override;
  bool setTSIGKey(const DNSName& name, const DNSName& algorithm, const string& content) override;
  bool deleteTSIGKey(const DNSName& name) override;
  bool getTSIGKeys(std::vector<struct TSIGKey>& keys) override;
  string directBackendCmd(const string& querystr) override;
  bool searchRecords(const string& pattern, size_t maxResults, vector<DNSResourceRecord>& result) override;
  bool searchComments(const string& pattern, size_t maxResults, vector<Comment>& result) override;
  void getAllDomains(vector<DomainInfo>* domains, bool getSerial, bool include_disabled) override;
  void getUpdatedPrimaries(vector<DomainInfo>& domains, std::unordered_set<DNSName>& catalogs, CatalogHashMap& catalogHashes) override;
  void getUnfreshSecondaryInfos(vector<DomainInfo>* domains) override;
  void setStale(uint32_t domain_id) override;
  void setFresh(uint32_t domain_id) override;

  static DNSBackend* maker();

private:
  int build();
  std::unique_ptr<Connector> connector;
  bool d_dnssec;
  Json d_result;
  int d_index{-1};
  int64_t d_trxid{0};
  std::string d_connstr;

  bool send(Json& value);
  bool recv(Json& value);
  static void makeErrorAndThrow(Json& value);

  static string asString(const Json& value)
  {
    if (value.is_number()) {
      return std::to_string(value.int_value());
    }
    if (value.is_bool()) {
      return (value.bool_value() ? "1" : "0");
    }
    if (value.is_string()) {
      return value.string_value();
    }
    throw JsonException("Json value not convertible to String");
  };

  static bool asBool(const Json& value)
  {
    if (value.is_bool()) {
      return value.bool_value();
    }
    try {
      string val = asString(value);
      if (val == "0") {
        return false;
      }
      if (val == "1") {
        return true;
      }
    }
    catch (const JsonException&) {
    };
    throw JsonException("Json value not convertible to boolean");
  };

  void parseDomainInfo(const json11::Json& obj, DomainInfo& di);
};
