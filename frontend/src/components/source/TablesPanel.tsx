'use client'

import { useState, useEffect, useCallback } from 'react'
import { sourcesApi } from '@/lib/api/sources'
import { SourceTableListItem, SourceTableDetailResponse } from '@/lib/types/api'
import { LoadingSpinner } from '@/components/common/LoadingSpinner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { TableIcon, ChevronLeft, ChevronRight, Copy, CheckCircle, AlertCircle } from 'lucide-react'

const PAGE_SIZE = 50

interface TablesPanelProps {
  sourceId: string
  tableCount: number
}

interface TableDetailState {
  data: SourceTableDetailResponse | null
  loading: boolean
  error: string | null
  page: number
}

/** Convert rows + headers to RFC 4180 CSV string */
function toCSV(headers: string[], rows: string[][]): string {
  const escape = (cell: string) => {
    if (cell.includes(',') || cell.includes('"') || cell.includes('\n')) {
      return `"${cell.replace(/"/g, '""')}"`
    }
    return cell
  }
  const lines: string[] = []
  lines.push(headers.map(escape).join(','))
  for (const row of rows) {
    lines.push(row.map(escape).join(','))
  }
  return lines.join('\r\n')
}

export function TablesPanel({ sourceId, tableCount }: TablesPanelProps) {
  const [tables, setTables] = useState<SourceTableListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Selected table + its detail state
  const [selectedTableId, setSelectedTableId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TableDetailState>({
    data: null,
    loading: false,
    error: null,
    page: 0,
  })

  // Per-table copy feedback
  const [copiedMd, setCopiedMd] = useState(false)
  const [copiedCsv, setCopiedCsv] = useState(false)

  // ── Load table list ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!sourceId || !(tableCount != null && tableCount > 0)) return
    let cancelled = false

    setLoading(true)
    setError(null)

    sourcesApi.getTables(sourceId).then((data) => {
      if (!cancelled) {
        setTables(data)
        setLoading(false)
      }
    }).catch(() => {
      if (!cancelled) {
        setError('Failed to load tables.')
        setLoading(false)
      }
    })

    return () => { cancelled = true }
  }, [sourceId, tableCount])

  // ── Load table detail (re-runs when selectedTableId or page changes) ─────────
  const fetchDetail = useCallback(async (tableId: string, page: number) => {
    setDetail(prev => ({ ...prev, loading: true, error: null }))
    try {
      const data = await sourcesApi.getTableDetail(
        sourceId,
        tableId,
        page * PAGE_SIZE,
        PAGE_SIZE,
      )
      setDetail({ data, loading: false, error: null, page })
    } catch {
      setDetail(prev => ({ ...prev, loading: false, error: 'Failed to load table rows.' }))
    }
  }, [sourceId])

  const handleSelectTable = useCallback((tableId: string) => {
    if (selectedTableId === tableId) {
      // Toggle off
      setSelectedTableId(null)
      setDetail({ data: null, loading: false, error: null, page: 0 })
      return
    }
    setSelectedTableId(tableId)
    setCopiedMd(false)
    setCopiedCsv(false)
    void fetchDetail(tableId, 0)
  }, [selectedTableId, fetchDetail])

  const handlePrev = () => {
    if (!selectedTableId || detail.page <= 0) return
    const newPage = detail.page - 1
    void fetchDetail(selectedTableId, newPage)
  }

  const handleNext = () => {
    if (!selectedTableId || !detail.data) return
    const totalPages = Math.ceil(detail.data.total_rows / PAGE_SIZE)
    if (detail.page >= totalPages - 1) return
    void fetchDetail(selectedTableId, detail.page + 1)
  }

  // ── Copy helpers ──────────────────────────────────────────────────────────────
  const handleCopyMarkdown = async () => {
    if (!detail.data?.markdown_repr) return
    try {
      await navigator.clipboard.writeText(detail.data.markdown_repr)
      setCopiedMd(true)
      setTimeout(() => setCopiedMd(false), 2000)
    } catch {
      // clipboard unavailable
    }
  }

  const handleCopyCSV = async () => {
    if (!detail.data) return
    const csv = toCSV(detail.data.column_headers, detail.data.rows)
    try {
      await navigator.clipboard.writeText(csv)
      setCopiedCsv(true)
      setTimeout(() => setCopiedCsv(false), 2000)
    } catch {
      // clipboard unavailable
    }
  }

  // ── Guard: hide panel entirely when no tables ─────────────────────────────────
  if (!(tableCount != null && tableCount > 0)) return null

  // ── Loading state ─────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center gap-2 py-4 text-muted-foreground">
        <LoadingSpinner size="sm" />
        <span className="text-sm">Loading tables…</span>
      </div>
    )
  }

  // ── Error state ───────────────────────────────────────────────────────────────
  if (error) {
    return (
      <p className="flex items-center gap-1.5 text-sm text-destructive py-2">
        <AlertCircle className="h-4 w-4 flex-shrink-0" />
        {error}
      </p>
    )
  }

  // ── Empty state ───────────────────────────────────────────────────────────────
  if (tables.length === 0) {
    return (
      <div className="text-center py-6 text-muted-foreground">
        <TableIcon className="h-10 w-10 mx-auto mb-2 opacity-40" />
        <p className="text-sm">No tables found in this source.</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 mb-1">
        <TableIcon className="h-4 w-4 text-muted-foreground" />
        <h3 className="text-sm font-semibold">Extracted Tables</h3>
        <Badge variant="secondary" className="text-xs">{tables.length}</Badge>
      </div>

      {tables.map((table) => {
        const isExpanded = selectedTableId === table.id

        return (
          <Card key={table.id} className="overflow-hidden">
            {/* ── Table list item header ── */}
            <CardHeader
              className="py-3 px-4 cursor-pointer hover:bg-muted/40 transition-colors"
              onClick={() => handleSelectTable(table.id)}
            >
              <CardTitle className="flex items-start justify-between text-sm font-medium">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">
                    Table {table.table_index + 1}
                    {table.sheet_name ? ` — ${table.sheet_name}` : ''}
                    {table.page_number != null ? ` (page ${table.page_number})` : ''}
                  </span>
                  <Badge variant="outline" className="text-xs">
                    {table.row_count} rows × {table.col_count} cols
                  </Badge>
                  {table.truncated && (
                    <Badge variant="destructive" className="text-xs">Truncated</Badge>
                  )}
                </div>
                <span className="text-xs text-muted-foreground ml-2 flex-shrink-0">
                  {isExpanded ? 'Collapse ▲' : 'Preview ▼'}
                </span>
              </CardTitle>

              {/* Column headers preview */}
              {table.column_headers.length > 0 && (
                <p className="mt-1 text-xs text-muted-foreground truncate">
                  Columns: {table.column_headers.slice(0, 8).join(', ')}
                  {table.column_headers.length > 8 ? ` +${table.column_headers.length - 8} more` : ''}
                </p>
              )}
            </CardHeader>

            {/* ── Expanded detail ── */}
            {isExpanded && (
              <CardContent className="px-4 pb-4 pt-0">
                {detail.loading && (
                  <div className="flex items-center gap-2 py-4">
                    <LoadingSpinner size="sm" />
                    <span className="text-sm text-muted-foreground">Loading rows…</span>
                  </div>
                )}

                {!detail.loading && detail.error && (
                  <p className="flex items-center gap-1.5 text-sm text-destructive py-2">
                    <AlertCircle className="h-4 w-4 flex-shrink-0" />
                    {detail.error}
                  </p>
                )}

                {!detail.loading && !detail.error && detail.data && (
                  <>
                    {/* Copy buttons */}
                    <div className="flex gap-2 mb-3">
                      <Button
                        size="sm"
                        variant="outline"
                        className="text-xs h-7"
                        onClick={handleCopyMarkdown}
                        disabled={!detail.data.markdown_repr}
                      >
                        {copiedMd
                          ? <><CheckCircle className="h-3 w-3 mr-1" />Copied!</>
                          : <><Copy className="h-3 w-3 mr-1" />Copy as Markdown</>
                        }
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        className="text-xs h-7"
                        onClick={handleCopyCSV}
                      >
                        {copiedCsv
                          ? <><CheckCircle className="h-3 w-3 mr-1" />Copied!</>
                          : <><Copy className="h-3 w-3 mr-1" />Copy as CSV</>
                        }
                      </Button>
                    </div>

                    {/* Row table */}
                    <div className="overflow-x-auto rounded border border-border">
                      <table className="min-w-full text-xs border-collapse">
                        <thead className="bg-muted">
                          <tr>
                            {detail.data.column_headers.map((h, i) => (
                              <th
                                key={i}
                                className="border-b border-border px-3 py-2 text-left font-semibold whitespace-nowrap"
                              >
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {detail.data.rows.length === 0 ? (
                            <tr>
                              <td
                                colSpan={detail.data.column_headers.length}
                                className="px-3 py-4 text-center text-muted-foreground"
                              >
                                No rows on this page.
                              </td>
                            </tr>
                          ) : (
                            detail.data.rows.map((row, ri) => (
                              <tr key={ri} className="border-b border-border last:border-0 odd:bg-muted/20">
                                {row.map((cell, ci) => (
                                  <td key={ci} className="px-3 py-1.5 whitespace-nowrap max-w-[240px] truncate">
                                    {cell}
                                  </td>
                                ))}
                              </tr>
                            ))
                          )}
                        </tbody>
                      </table>
                    </div>

                    {/* Pagination */}
                    {detail.data.total_rows > PAGE_SIZE && (
                      <div className="flex items-center justify-between mt-3">
                        <span className="text-xs text-muted-foreground">
                          Rows {detail.page * PAGE_SIZE + 1}–
                          {Math.min((detail.page + 1) * PAGE_SIZE, detail.data.total_rows)} of{' '}
                          {detail.data.total_rows}
                        </span>
                        <div className="flex gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2"
                            onClick={handlePrev}
                            disabled={detail.page <= 0}
                          >
                            <ChevronLeft className="h-3 w-3" />
                            Prev
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2"
                            onClick={handleNext}
                            disabled={(detail.page + 1) * PAGE_SIZE >= detail.data.total_rows}
                          >
                            Next
                            <ChevronRight className="h-3 w-3" />
                          </Button>
                        </div>
                      </div>
                    )}
                  </>
                )}
              </CardContent>
            )}
          </Card>
        )
      })}
    </div>
  )
}
