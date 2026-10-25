-- a small vector/queue/class library plus a JSON-ish encoder
local Queue = {}
Queue.__index = Queue

function Queue.new()
  return setmetatable({ first = 1, last = 0, items = {} }, Queue)
end

function Queue:push(value)
  self.last = self.last + 1
  self.items[self.last] = value
end

function Queue:pop()
  if self.first > self.last then return nil end
  local value = self.items[self.first]
  self.items[self.first] = nil
  self.first = self.first + 1
  return value
end

function Queue:size()
  return self.last - self.first + 1
end

local q = Queue.new()
for i = 1, 5 do q:push(i * i) end
local total = 0
while true do
  local v = q:pop()
  if not v then break end
  total = total + v
end
print("queue total", total, q:size())

local function escape(s)
  return (s:gsub('[%c"\\]', function(ch)
    local map = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\t"] = "\\t" }
    return map[ch] or string.format("\\%03d", ch:byte())
  end))
end

local function encode(value, indent)
  indent = indent or ""
  local kind = type(value)
  if kind == "table" then
    local is_array = #value > 0
    local parts = {}
    local pad = indent .. "  "
    for key, item in pairs(value) do
      if is_array then
        parts[#parts + 1] = pad .. encode(item, pad)
      else
        parts[#parts + 1] = pad .. '"' .. escape(tostring(key)) .. '": ' .. encode(item, pad)
      end
    end
    table.sort(parts)
    if is_array then
      return "[\n" .. table.concat(parts, ",\n") .. "\n" .. indent .. "]"
    end
    return "{\n" .. table.concat(parts, ",\n") .. "\n" .. indent .. "}"
  elseif kind == "string" then
    return '"' .. escape(value) .. '"'
  elseif kind == "number" or kind == "boolean" then
    return tostring(value)
  else
    return "null"
  end
end

print(encode({ name = "demo", count = 3, ok = true, tags = { "a", "b" } }))
print(encode({ nested = { deep = { 1, 2, 3 } } }))

-- memoised fibonacci
local memo = {}
local function fib(n)
  if n < 2 then return n end
  if memo[n] then return memo[n] end
  local value = fib(n - 1) + fib(n - 2)
  memo[n] = value
  return value
end
print("fib", fib(60))

-- string building
local parts = {}
for i = 1, 10 do parts[#parts + 1] = tostring(i) end
print(table.concat(parts, "-"))
print((""):rep(0) == "", string.len("abc"))
