import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow
} from "@/components/ui/table"
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger
} from "@/components/ui/dropdown-menu"
import {
  HoverCard, HoverCardContent, HoverCardTrigger
} from "@/components/ui/hover-card"
import {
  Tooltip, TooltipContent, TooltipTrigger
} from "@/components/ui/tooltip"
import { useState } from "react"
import {
  ChevronDown, ChevronRight, Search, UserX, Star, Check,
  MoreHorizontal, ExternalLink, Mail, Phone, Globe, Package,
  FileText, User, Calendar, Hash, ClipboardList, Info
} from "lucide-react"

/* ------------------------------------------------------------------ */
/*  Status helpers                                                     */
/* ------------------------------------------------------------------ */

const itemStatusConfig = {
  confirmed:    { label: "Confirmed",    dotStyle: { backgroundColor: "#22c55e" } },
  identified:   { label: "Identified",   dotStyle: { backgroundColor: "#3b82f6" } },
  unidentified: { label: "Unidentified", dotStyle: { backgroundColor: "#94a3b8" } },
}

const supplierStatusConfig = {
  candidate:          { label: "Candidate",    color: "bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200" },
  estimated:          { label: "Est",          color: "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200" },
  previous_purchase:  { label: "Prev Purch",   color: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200" },
  previous_quote:     { label: "Prev Quote",   color: "bg-cyan-100 text-cyan-800 dark:bg-cyan-900 dark:text-cyan-200" },
  quoted:             { label: "Quoted",       color: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" },
  shortlisted:        { label: "Shortlisted",  color: "bg-violet-100 text-violet-800 dark:bg-violet-900 dark:text-violet-200" },
  selected:           { label: "Selected",     color: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200" },
  dropped:            { label: "Dropped",      color: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200 line-through" },
}

const rfqStatusConfig = {
  draft:            { label: "Draft",           color: "bg-slate-100 text-slate-700" },
  in_progress:      { label: "In Progress",     color: "bg-blue-100 text-blue-800" },
  awaiting_quotes:  { label: "Awaiting Quotes", color: "bg-amber-100 text-amber-800" },
  completed:        { label: "Completed",       color: "bg-green-100 text-green-800" },
  cancelled:        { label: "Cancelled",       color: "bg-red-100 text-red-800" },
}

function formatPrice(price) {
  if (price == null) return null
  return `$${Number(price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/* ------------------------------------------------------------------ */
/*  Supplier row                                                       */
/* ------------------------------------------------------------------ */

function SupplierRow({ supplier, rfqId, line }) {
  const st = supplierStatusConfig[supplier.status] || supplierStatusConfig.candidate
  const isDropped = supplier.status === "dropped"

  const contacts = supplier.contacts || []
  const hasDetails = contacts.length > 0 || supplier.lead_time || supplier.notes

  const nameText = (
    <span className={`text-xs ${isDropped ? "line-through opacity-50" : ""}`}>
      {supplier.name}
    </span>
  )

  return (
    <tr className="border-b last:border-b-0" style={{ borderColor: "rgba(128,128,128,0.15)" }}>
      <td className="py-1 pr-6">
        {hasDetails ? (
          <HoverCard>
            <HoverCardTrigger asChild>
              <button
                className="text-left cursor-pointer flex items-center gap-1"
                style={{ textDecoration: "underline dotted", textUnderlineOffset: "3px", textDecorationColor: "rgba(150,150,150,0.5)" }}
              >
                {nameText}
                <Info className="h-3 w-3 opacity-30 shrink-0" />
              </button>
            </HoverCardTrigger>
            <HoverCardContent className="w-72 text-xs" side="top">
              <div className="space-y-2">
                <p className="font-semibold text-sm">{supplier.name}</p>
                {contacts.map(function(c, i) {
                  return (
                    <div key={i} className="flex items-center gap-1.5">
                      {c.type === "email" && <Mail className="h-3 w-3 opacity-60" />}
                      {c.type === "phone" && <Phone className="h-3 w-3 opacity-60" />}
                      {c.type === "url" && <Globe className="h-3 w-3 opacity-60" />}
                      <span>{c.value}</span>
                    </div>
                  )
                })}
                {supplier.lead_time && (
                  <div className="flex items-center gap-1.5">
                    <Calendar className="h-3 w-3 opacity-60" />
                    <span>{supplier.lead_time}</span>
                  </div>
                )}
                {supplier.notes && (
                  <div className="flex items-center gap-1.5">
                    <FileText className="h-3 w-3 opacity-60" />
                    <span className="italic">{supplier.notes}</span>
                  </div>
                )}
              </div>
            </HoverCardContent>
          </HoverCard>
        ) : nameText}
      </td>
      <td className="py-1 pr-6 text-xs text-right font-medium" style={{ whiteSpace: "nowrap" }}>
        {supplier.price != null ? (
          <span className={isDropped ? "line-through opacity-50" : ""}>
            {formatPrice(supplier.price)}
          </span>
        ) : (
          <span className="opacity-30">—</span>
        )}
      </td>
      <td className="py-1 pr-6">
        <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${st.color}`} style={{ whiteSpace: "nowrap" }}>
          {st.label}
        </span>
      </td>
      <td className="py-1 w-6">
        {!isDropped && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="p-0.5 rounded hover:bg-muted cursor-pointer">
                <MoreHorizontal className="h-3.5 w-3.5 opacity-50" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="text-xs">
              {supplier.status !== "shortlisted" && (
                <DropdownMenuItem
                  onClick={function() {
                    callAction({ name: "rfq_update_supplier", payload: { rfq_id: rfqId, line: line, supplier_name: supplier.name, status: "shortlisted" }})
                  }}
                >
                  <Star className="h-3 w-3 mr-1.5" /> Shortlist
                </DropdownMenuItem>
              )}
              {supplier.status !== "selected" && (
                <DropdownMenuItem
                  onClick={function() {
                    callAction({ name: "rfq_update_supplier", payload: { rfq_id: rfqId, line: line, supplier_name: supplier.name, status: "selected" }})
                  }}
                >
                  <Check className="h-3 w-3 mr-1.5" /> Select
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                className="text-destructive"
                onClick={function() {
                  callAction({ name: "rfq_update_supplier", payload: { rfq_id: rfqId, line: line, supplier_name: supplier.name, status: "dropped" }})
                }}
              >
                <UserX className="h-3 w-3 mr-1.5" /> Drop
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </td>
    </tr>
  )
}

/* ------------------------------------------------------------------ */
/*  Item row                                                           */
/* ------------------------------------------------------------------ */

function ItemRow({ item, rfqId, expanded, onToggle }) {
  const suppliers = item.suppliers || []
  const stCfg = itemStatusConfig[item.status] || itemStatusConfig.unidentified

  return (
    <>
      <TableRow
        className="cursor-pointer hover:bg-muted/50"
        onClick={onToggle}
      >
        <TableCell className="w-8 text-center font-mono text-xs">
          {item.line}
        </TableCell>
        <TableCell>
          <div className="flex items-center gap-1.5">
            {suppliers.length > 0 ? (
              expanded
                ? <ChevronDown className="h-3.5 w-3.5 opacity-40 shrink-0" />
                : <ChevronRight className="h-3.5 w-3.5 opacity-40 shrink-0" />
            ) : <span className="w-3.5 shrink-0" />}
            <span className="text-sm">{item.input_description || "—"}</span>
          </div>
        </TableCell>
        <TableCell className="text-sm font-mono">
          {item.part_number || item.input_code || "—"}
        </TableCell>
        <TableCell className="text-sm">
          {item.brand || "—"}
        </TableCell>
        <TableCell className="text-sm text-center">
          {item.quantity ? `${item.quantity} ${item.uom || "ea"}` : "—"}
        </TableCell>
        <TableCell className="text-center">
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                style={Object.assign({ display: "inline-block", width: 10, height: 10, borderRadius: "50%" }, stCfg.dotStyle)}
              />
            </TooltipTrigger>
            <TooltipContent>{stCfg.label}</TooltipContent>
          </Tooltip>
        </TableCell>
        <TableCell className="text-center text-sm">
          {suppliers.length > 0 ? (
            <span className="tabular-nums">{suppliers.filter(function(s) { return s.status !== "dropped" }).length}</span>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  className="p-1 rounded hover:bg-muted cursor-pointer"
                  onClick={function(e) {
                    e.stopPropagation()
                    sendUserMessage("Find suppliers for line " + item.line + " of " + rfqId)
                  }}
                >
                  <Search className="h-3.5 w-3.5 opacity-40" />
                </button>
              </TooltipTrigger>
              <TooltipContent>Find suppliers</TooltipContent>
            </Tooltip>
          )}
        </TableCell>
      </TableRow>
      {expanded && suppliers.length > 0 && (
        <TableRow>
          <TableCell />
          <TableCell colSpan={6} className="py-2 pl-8 pr-4">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[10px] text-muted-foreground uppercase tracking-wide font-medium">Suppliers</span>
              <button
                className="text-xs text-muted-foreground hover:text-foreground cursor-pointer flex items-center gap-1"
                onClick={function() {
                  sendUserMessage("Find more suppliers for line " + item.line + " of " + rfqId)
                }}
              >
                <Search className="h-3 w-3" /> Find more
              </button>
            </div>
            <table className="w-full">
              <tbody>
                {suppliers.map(function(s, i) {
                  return (
                    <SupplierRow
                      key={s.name + "-" + i}
                      supplier={s}
                      rfqId={rfqId}
                      line={item.line}
                    />
                  )
                })}
              </tbody>
            </table>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
/*  Main component                                                     */
/* ------------------------------------------------------------------ */

export default function RFQSummary() {
  const rfqId = props.id || "???"
  const customer = props.customer || "Unknown"
  const status = props.status || "draft"
  const assigned = props.assigned_to || "Unassigned"
  const created = props.created_date || ""
  const reference = props.reference || ""
  const netsuite = props.netsuite_opportunity || ""
  const hubspot = props.hubspot_deal || ""
  const contact = props.customer_contact || null
  const notes = props.notes || ""
  const items = props.items || []

  const stCfg = rfqStatusConfig[status] || rfqStatusConfig.draft

  const confirmed = items.filter(function(i) { return i.status === "confirmed" }).length
  const identified = items.filter(function(i) { return i.status === "identified" }).length
  const unidentified = items.filter(function(i) { return i.status === "unidentified" }).length
  const withSuppliers = items.filter(function(i) { return (i.suppliers || []).length > 0 }).length

  const [expandedRows, setExpandedRows] = useState({})

  function toggleRow(line) {
    setExpandedRows(function(prev) {
      const next = Object.assign({}, prev)
      next[line] = !prev[line]
      return next
    })
  }

  function expandAll() {
    const next = {}
    items.forEach(function(i) { next[i.line] = true })
    setExpandedRows(next)
  }

  function collapseAll() {
    setExpandedRows({})
  }

  const allExpanded = items.length > 0 && items.every(function(i) { return expandedRows[i.line] })

  return (
    <Card className="w-full my-2">
      <CardHeader className="pb-3 pt-4 px-4">
        <div className="flex items-start justify-between gap-2">
          <div className="space-y-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <ClipboardList className="h-5 w-5 opacity-60 shrink-0" />
              <CardTitle className="text-lg font-semibold">{rfqId}</CardTitle>
              <span className="text-muted-foreground">—</span>
              <span className="text-lg font-medium">{customer}</span>
            </div>
            <div className="flex items-center gap-3 text-xs text-muted-foreground flex-wrap">
              <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${stCfg.color}`}>
                {stCfg.label}
              </span>
              <span className="flex items-center gap-1">
                <User className="h-3 w-3" /> {assigned}
              </span>
              {created && (
                <span className="flex items-center gap-1">
                  <Calendar className="h-3 w-3" /> {created}
                </span>
              )}
              {reference && (
                <span className="flex items-center gap-1">
                  <Hash className="h-3 w-3" /> {reference}
                </span>
              )}
            </div>
            {contact && (contact.name || contact.email || contact.phone) && (
              <div className="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
                {contact.name && <span>{contact.name}</span>}
                {contact.email && (
                  <span className="flex items-center gap-1.5">
                    <Mail className="h-3 w-3" /> {contact.email}
                  </span>
                )}
                {contact.phone && (
                  <span className="flex items-center gap-1.5">
                    <Phone className="h-3 w-3" /> {contact.phone}
                  </span>
                )}
              </div>
            )}
            {(netsuite || hubspot) && (
              <div className="flex items-center gap-3 text-xs text-muted-foreground">
                {netsuite && <span className="flex items-center gap-1"><ExternalLink className="h-3 w-3" /> NetSuite: {netsuite}</span>}
                {hubspot && <span className="flex items-center gap-1"><ExternalLink className="h-3 w-3" /> HubSpot: {hubspot}</span>}
              </div>
            )}
            {notes && (
              <p className="text-xs text-muted-foreground italic">{notes}</p>
            )}
          </div>
        </div>
      </CardHeader>

      <CardContent className="px-4 pb-4">
        {items.length > 0 ? (
          <>
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <span className="font-medium">{items.length} items</span>
                <span>·</span>
                {confirmed > 0 && <span>{confirmed} confirmed</span>}
                {identified > 0 && <span>{identified} identified</span>}
                {unidentified > 0 && <span className="text-amber-600">{unidentified} unidentified</span>}
                <span>·</span>
                <span>{withSuppliers} with suppliers</span>
              </div>
              <div className="flex items-center gap-1">
                <button
                  className="text-[11px] text-muted-foreground hover:text-foreground px-1.5 py-0.5 rounded hover:bg-muted cursor-pointer"
                  onClick={allExpanded ? collapseAll : expandAll}
                >
                  {allExpanded ? "Collapse all" : "Expand all"}
                </button>
              </div>
            </div>

            <div className="border rounded-md overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-8 text-center">#</TableHead>
                    <TableHead>Description</TableHead>
                    <TableHead>Part Number</TableHead>
                    <TableHead>Brand</TableHead>
                    <TableHead className="text-center">Qty</TableHead>
                    <TableHead className="text-center w-12">Status</TableHead>
                    <TableHead className="text-center w-16">Sup</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map(function(item) {
                    return (
                      <ItemRow
                        key={item.line}
                        item={item}
                        rfqId={rfqId}
                        expanded={!!expandedRows[item.line]}
                        onToggle={function() { toggleRow(item.line) }}
                      />
                    )
                  })}
                </TableBody>
              </Table>
            </div>

            <div className="flex gap-2 mt-4 pt-2">
              {unidentified > 0 && (
                <Button
                  variant="outline"
                  size="default"
                  className="text-sm"
                  onClick={function() {
                    sendUserMessage("Identify all unidentified items in " + rfqId)
                  }}
                >
                  <Package className="h-4 w-4 mr-1.5" /> Identify items
                </Button>
              )}
              <Button
                variant="outline"
                size="default"
                className="text-sm"
                onClick={function() {
                  sendUserMessage("Find suppliers for all confirmed items in " + rfqId)
                }}
              >
                <Search className="h-4 w-4 mr-1.5" /> Find suppliers
              </Button>
            </div>
          </>
        ) : (
          <p className="text-sm text-muted-foreground italic">No items yet.</p>
        )}
      </CardContent>
    </Card>
  )
}
