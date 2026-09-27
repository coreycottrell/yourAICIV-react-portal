export interface Doc {
  id: string
  title: string
  content: string
  visibility: string
  tags: string[]
  author?: string
  created_at?: string
  updated_at?: string
}

export interface CreateDocRequest {
  title: string
  content: string
  visibility: string
  tags: string[]
}

export interface UpdateDocRequest {
  title?: string
  content?: string
  visibility?: string
  tags?: string[]
}
