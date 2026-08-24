spawn(function()
    while task.wait(30) do -- Cứ 30 giây gửi tín hiệu sống 1 lần
        pcall(function()
            -- Ghi thời gian hiện tại vào file ping.txt
            writefile("ping.txt", tostring(os.time()))
        end)
    end
end)
