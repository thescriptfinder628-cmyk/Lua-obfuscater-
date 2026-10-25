local a = 1
local b = 2
local c = a + b * 3 - 4 / 2
print(a, b, c)

local t = { 1, 2, 3, name = "demo" }
t.extra = t[1] + t[3]
t["quoted key"] = true
print(t.name, t.extra, t["quoted key"], #t)

local s = "hello" .. " " .. "world"
print(s, s:upper(), #s, s:sub(2, 4))

if a < b then print("less") elseif a == b then print("equal") else print("more") end

local i = 0
while i < 3 do i = i + 1 end
print("i", i)
repeat i = i - 1 until i == 0
print("i", i)

for n = 1, 5 do io_n = n end
print("last n", io_n)

print(type(nil), type(true), type(1), type("s"), type({}), type(print))
print(1 == 1.0, "1" == 1, not nil, nil == false)
