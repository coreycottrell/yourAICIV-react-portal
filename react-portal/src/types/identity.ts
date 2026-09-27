export interface StatusResponse {
  civ: string
  uptime: number
  tmux_session: string
  tmux_alive: boolean
  claude_running: boolean
  ctx_pct: number | null
  timestamp: number
  version: string
}

export interface CommandsResponse {
  server: {
    hostname: string
    server_ip: string
    ssh_port: string
    ssh_user: string
    portal_url: string
  }
  paths: {
    home: string
    civ_root: string
    portal_dir: string
    tools_dir: string
    logs_dir: string
  }
  tmux: {
    primary_session: string
  }
  civ: {
    name: string
    human_name: string
  }
  owner: {
    name: string
    email: string
  }
}
